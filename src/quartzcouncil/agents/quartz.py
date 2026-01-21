from __future__ import annotations
import asyncio
import os

from pydantic import BaseModel

from quartzcouncil.core.types import ReviewComment
from quartzcouncil.core.pr_models import PullRequestInput
from quartzcouncil.agents.amethyst import review_amethyst
from quartzcouncil.agents.citrine import review_citrine


class CouncilReview(BaseModel):
    """Final output from the Quartz council."""
    comments: list[ReviewComment]
    summary: str


def _comments_overlap(
    new_comment: ReviewComment,
    existing_comment: ReviewComment,
) -> bool:
    """
    Return True if two review comments overlap in the same file.
    Overlap means they share at least one line.
    """

    # Comments in different files can never overlap
    if new_comment.file != existing_comment.file:
        return False

    new_starts_after_existing = new_comment.line_start > existing_comment.line_end
    new_ends_before_existing = new_comment.line_end < existing_comment.line_start

    # If neither condition is true, they overlap
    return not (new_starts_after_existing or new_ends_before_existing)


def _deduplicate(comments: list[ReviewComment], max_comments: int = 20) -> list[ReviewComment]:
    """
    Deduplicate overlapping comments, preferring higher severity.
    Limits total comments to avoid noisy reviews.
    """
    severity_rank = {"error": 3, "warning": 2, "info": 1}

    sorted_comments = sorted(
        comments,
        key=lambda comment: (-severity_rank[comment.severity], comment.file, comment.line_start)
    )

    kept: list[ReviewComment] = []
    for comment in sorted_comments:
        overlaps = any(_comments_overlap(comment, existing_comment) for existing_comment in kept)
        if not overlaps:
            kept.append(comment)
        if len(kept) >= max_comments:
            break

    return kept


def _generate_summary(comments: list[ReviewComment]) -> str:
    """Generate a summary of the review."""
    if not comments:
        return "No issues found. The code looks good."

    error_count = sum(1 for comment in comments if comment.severity == "error")
    warning_count = sum(1 for comment in comments if comment.severity == "warning")
    info_count = sum(1 for comment in comments if comment.severity == "info")

    by_category: dict[str, int] = {}
    for comment in comments:
        by_category[comment.category] = by_category.get(comment.category, 0) + 1

    if error_count > 0:
        risk = "HIGH"
    elif warning_count > 2:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    lines = [
        f"**Risk Level:** {risk}",
        "",
        f"**Issues Found:** {len(comments)} total",
        f"- Errors: {error_count}",
        f"- Warnings: {warning_count}",
        f"- Info: {info_count}",
        "",
        "**By Category:**",
    ]
    for category, count in sorted(by_category.items()):
        lines.append(f"- {category}: {count}")

    error_comments = [comment for comment in comments if comment.severity == "error"]
    if error_comments:
        lines.append("")
        lines.append("**Top Concerns:**")
        for error in error_comments[:3]:
            lines.append(f"- [{error.file}:{error.line_start}] {error.message[:80]}...")

    return "\n".join(lines)


async def review_council(
    pr: PullRequestInput,
    max_comments: int = 20,
) -> CouncilReview:
    """
    Run the Quartz council review.

    Executes Amethyst and Citrine in parallel, deduplicates overlapping
    comments, and generates a summary.
    """
    # Run both agents in parallel
    amethyst_comments, citrine_comments = await asyncio.gather(
        review_amethyst(pr),
        review_citrine(pr),
    )

    all_comments = amethyst_comments + citrine_comments
    final_comments = _deduplicate(all_comments, max_comments=max_comments)
    summary = _generate_summary(final_comments)

    return CouncilReview(comments=final_comments, summary=summary)
