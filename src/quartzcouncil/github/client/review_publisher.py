from __future__ import annotations

from quartzcouncil.core.types import ReviewComment
from quartzcouncil.core.pr_models import PullRequestFile
from quartzcouncil.github.client.github_client import GitHubClient
from quartzcouncil.github.client.diff_parser import build_file_line_map, snap_to_nearest_valid_line


def format_inline_comment(comment: ReviewComment) -> str:
    severity_label = comment.severity.upper()
    header = f"**{comment.agent}** · **{severity_label}** · `{comment.category}`"
    body = comment.message.strip()
    if comment.suggestion:
        body += f"\n\n**Suggestion:**\n{comment.suggestion.strip()}"
    return f"{header}\n\n{body}"


def format_summary_comment(comment: ReviewComment) -> str:
    """Format a comment for inclusion in the summary (not inline)."""
    severity_label = comment.severity.upper()
    header = f"**{comment.agent}** · **{severity_label}** · `{comment.category}`"
    location = f"`{comment.file}:{comment.line_start}`"
    body = comment.message.strip()
    if comment.suggestion:
        body += f"\n  **Suggestion:** {comment.suggestion.strip()}"
    return f"- {location} — {header}\n  {body}"


def format_summary(
    summary_md: str,
    posted: int,
    skipped_comments: list[ReviewComment],
) -> str:
    lines = [
        "## QuartzCouncil Review",
        "",
        summary_md.strip(),
        "",
        f"**Inline comments posted:** {posted}",
    ]

    if skipped_comments:
        lines.append(f"**Additional comments ({len(skipped_comments)}):**")
        lines.append("")
        for comment in skipped_comments:
            lines.append(format_summary_comment(comment))
        lines.append("")

    lines.append("_Triggered via `/quartz review`_")
    return "\n".join(lines)


def to_github_review_comment(comment: ReviewComment) -> dict:
    """
    Format a ReviewComment for GitHub's review API.

    Note: commit_id is NOT included here - it goes at the top level of the review,
    not inside each comment object.
    """
    return {
        "path": comment.file,
        "line": comment.line_start,
        "side": "RIGHT",
        "body": format_inline_comment(comment),
    }


async def create_pr_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    summary_md: str,
    comments: list[ReviewComment],
    files: list[PullRequestFile],
    gh: GitHubClient,
    max_inline: int = 20,
) -> dict:
    """
    Best-effort publishing:
    - validates comment line numbers against actual diff hunks
    - only includes comments that can be anchored to valid diff lines
    - if GitHub rejects inline payload (422), retry with summary only
    """
    # Build map of valid line numbers per file from diff patches
    file_dicts = [{"filename": f.filename, "patch": f.patch} for f in files]
    file_line_map = build_file_line_map(file_dicts)

    # Snap comments to nearest valid line in the diff
    valid_comments: list[ReviewComment] = []
    unmappable_comments: list[ReviewComment] = []

    for comment in comments:
        snapped_line = snap_to_nearest_valid_line(comment.file, comment.line_start, file_line_map)
        if snapped_line is not None:
            snapped_comment = comment.model_copy(update={"line_start": snapped_line})
            valid_comments.append(snapped_comment)
        else:
            unmappable_comments.append(comment)

    # Take up to max_inline for inline posting, rest go to summary
    inline_comments = valid_comments[:max_inline]
    overflow_comments = valid_comments[max_inline:]
    skipped_comments = unmappable_comments + overflow_comments

    inline_payload = [to_github_review_comment(comment) for comment in inline_comments]

    request_body = {
        "event": "COMMENT",
        "body": format_summary(summary_md, posted=len(inline_payload), skipped_comments=skipped_comments),
        "commit_id": commit_id,
        "comments": inline_payload,
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

    try:
        return await gh.post_json(url, request_body)
    except Exception:
        # Inline comments failed (likely 422) - fall back to summary-only
        fallback = {
            "event": "COMMENT",
            "body": format_summary(summary_md, posted=0, skipped_comments=comments),
            "commit_id": commit_id,
        }
        return await gh.post_json(url, fallback)
