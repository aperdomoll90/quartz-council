from __future__ import annotations

from quartzcouncil.core.types import ReviewComment
from quartzcouncil.core.pr_models import PullRequestFile
from quartzcouncil.github.client.github_client import GitHubClient
from quartzcouncil.github.client.diff_parser import build_file_line_map, snap_to_nearest_valid_line, extract_line_from_patch


def format_inline_comment(comment: ReviewComment, code_snippet: str | None = None) -> str:
    severity_label = comment.severity.upper()
    header = f"**{comment.agent}** · **{severity_label}** · `{comment.category}`"

    parts = [header, ""]

    # Add code snippet if available
    if code_snippet:
        parts.append(f"```typescript\n{code_snippet.strip()}\n```")
        parts.append("")

    parts.append(comment.message.strip())

    if comment.suggestion:
        parts.append("")
        parts.append(f"**Suggestion:**\n{comment.suggestion.strip()}")

    return "\n".join(parts)


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


def to_github_review_comment(comment: ReviewComment, code_snippet: str | None = None) -> dict:
    """
    Format a ReviewComment for GitHub's review API.

    Note: commit_id is NOT included here - it goes at the top level of the review,
    not inside each comment object.
    """
    return {
        "path": comment.file,
        "line": comment.line_start,
        "side": "RIGHT",
        "body": format_inline_comment(comment, code_snippet),
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
    file_dicts = [{"filename": pr_file.filename, "patch": pr_file.patch} for pr_file in files]
    file_line_map = build_file_line_map(file_dicts)

    # Build patch map for extracting code snippets
    patch_by_filename = {pr_file.filename: pr_file.patch for pr_file in files}

    # Snap comments to nearest valid line in the diff
    valid_comments: list[ReviewComment] = []
    unmappable_comments: list[ReviewComment] = []

    for review_comment in comments:
        original_line = review_comment.line_start
        snapped_line = snap_to_nearest_valid_line(review_comment.file, original_line, file_line_map)

        if snapped_line is not None:
            # Log if we had to snap to a different line
            if snapped_line != original_line:
                print(f"[QuartzCouncil] 📍 Snapped {review_comment.file}:{original_line} → {snapped_line}")
            snapped_comment = review_comment.model_copy(update={"line_start": snapped_line})
            valid_comments.append(snapped_comment)
        else:
            print(f"[QuartzCouncil] ⚠️ Could not map {review_comment.file}:{original_line} to valid diff line")
            unmappable_comments.append(review_comment)

    # Take up to max_inline for inline posting, rest go to summary
    inline_comments = valid_comments[:max_inline]
    overflow_comments = valid_comments[max_inline:]
    skipped_comments = unmappable_comments + overflow_comments

    # Build inline payload with code snippets
    inline_payload = []
    for inline_comment in inline_comments:
        file_patch = patch_by_filename.get(inline_comment.file, "")
        code_snippet = extract_line_from_patch(file_patch, inline_comment.line_start)
        inline_payload.append(to_github_review_comment(inline_comment, code_snippet))

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
