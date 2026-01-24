from __future__ import annotations
import asyncio
from pathlib import Path

from pydantic import BaseModel

from quartzcouncil.core.types import ReviewComment, ReviewWarning
from quartzcouncil.core.pr_models import PullRequestInput, PullRequestFile
from quartzcouncil.agents.amethyst import review_amethyst
from quartzcouncil.agents.citrine import review_citrine


# =============================================================================
# FILE TYPE ROUTING
# =============================================================================
# Currently permissive - agents see files they might be able to review.
# This is a wireframe for tightening later.
#
# TODO: Tighten these filters as we learn what each agent handles well:
# - Amethyst could potentially exclude .js (non-typed) or config files
# - Citrine could exclude non-component files (utils, constants, etc.)
# - Consider adding content-based detection (e.g., "use client" directive)
# =============================================================================

# Amethyst: TypeScript type safety reviewer
# Permissive: includes JS/JSX since they may have JSDoc types or be migrated
# TODO: For now includes .py for testing on this repo - remove later
AMETHYST_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".py"}

# Citrine: React/Next.js reviewer
# Permissive: same as Amethyst since React can be in any JS/TS file
# TODO: Could detect React imports or hooks to filter more precisely
# TODO: For now includes .py for testing on this repo - remove later
CITRINE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".py"}


def _filter_files_for_agent(
    files: list[PullRequestFile],
    extensions: set[str],
) -> list[PullRequestFile]:
    """Filter PR files to only those matching the given extensions."""
    return [
        pr_file for pr_file in files
        if Path(pr_file.filename).suffix.lower() in extensions
    ]


class CouncilReview(BaseModel):
    """Final output from the Quartz council."""
    comments: list[ReviewComment]
    warnings: list[ReviewWarning]
    summary: str


# =============================================================================
# MODERATOR SANITY GATE
# =============================================================================
# The LLM doesn't always follow prompt rules. This gate enforces quality:
# 1. Hedging words in ERROR → downgrade to WARNING
# 2. Known false positives → DROP entirely
# 3. Speculative claims without evidence → downgrade
# =============================================================================

HEDGE_WORDS = (
    " may ", " may,", " may.", "might", "could", "potential", "possibly", "likely",
    "suggest", "arguably", "leading to",
)

# Known false positive patterns: (agent, keyword1, keyword2) → drop if both keywords present
# These are claims the LLM makes confidently but are factually wrong
KNOWN_FALSE_POSITIVES = [
    # next/image is allowed in client components - Citrine gets this wrong
    ("Citrine", "next/image", "server-only"),
    ("Citrine", "next/image", "server only"),
    # Image component is not server-only
    ("Citrine", "image", "server-only api"),
    # "ensure" comments are advice, not errors
    ("Citrine", "ensure", None),
    ("Amethyst", "ensure", None),
]


def _sanitize_comments(comments: list[ReviewComment]) -> list[ReviewComment]:
    """
    Moderator sanity gate: enforce quality rules that LLM may not follow.

    Rules applied:
    1. If ERROR contains hedging words → downgrade to WARNING
    2. If comment matches known false positive → DROP
    3. If message is pure speculation → downgrade

    This runs BEFORE deduplication so we filter junk first.
    """
    sanitized: list[ReviewComment] = []
    dropped_count = 0
    downgraded_count = 0

    for comment in comments:
        message_lower = comment.message.lower()

        # Rule 2: Drop known false positives
        should_drop = False
        for agent_name, keyword1, keyword2 in KNOWN_FALSE_POSITIVES:
            if comment.agent == agent_name:
                if keyword1 in message_lower:
                    # If keyword2 is None, only keyword1 needs to match
                    if keyword2 is None or keyword2 in message_lower:
                        should_drop = True
                        break

        if should_drop:
            dropped_count += 1
            continue

        # Rule 1: Hedging words cannot be ERROR
        if comment.severity == "error":
            has_hedge = any(hedge in message_lower for hedge in HEDGE_WORDS)
            if has_hedge:
                # Create new comment with downgraded severity
                comment = ReviewComment(
                    file=comment.file,
                    line_start=comment.line_start,
                    line_end=comment.line_end,
                    severity="warning",
                    category=comment.category,
                    message=comment.message,
                    suggestion=comment.suggestion,
                    agent=comment.agent,
                )
                downgraded_count += 1

        sanitized.append(comment)

    if dropped_count > 0 or downgraded_count > 0:
        print(f"[QuartzCouncil] 🧹 Moderator: dropped {dropped_count}, downgraded {downgraded_count} comments")

    return sanitized


def _comments_overlap_by_lines(
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


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text for similarity comparison."""
    # Common words to ignore
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "shall", "can", "need", "to", "of",
        "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
        "this", "that", "these", "those", "it", "its", "and", "or", "but", "if",
        "then", "else", "when", "where", "which", "who", "what", "how", "why",
        "not", "no", "yes", "all", "any", "each", "every", "both", "few", "more",
        "most", "other", "some", "such", "only", "same", "so", "than", "too",
        "very", "just", "also", "here", "there", "now", "then", "once", "already",
    }

    # Extract words, lowercase, filter
    words = set()
    for word in text.lower().split():
        # Remove punctuation
        cleaned = "".join(char for char in word if char.isalnum())
        if cleaned and len(cleaned) > 2 and cleaned not in stopwords:
            words.add(cleaned)

    return words


def _comments_similar_by_content(
    new_comment: ReviewComment,
    existing_comment: ReviewComment,
    similarity_threshold: float = 0.5,
) -> bool:
    """
    Return True if two comments have similar content based on keyword overlap.
    Uses Jaccard similarity on extracted keywords.
    """
    new_keywords = _extract_keywords(new_comment.message)
    existing_keywords = _extract_keywords(existing_comment.message)

    if not new_keywords or not existing_keywords:
        return False

    intersection = new_keywords & existing_keywords
    union = new_keywords | existing_keywords

    similarity = len(intersection) / len(union) if union else 0

    return similarity >= similarity_threshold


def _is_duplicate_comment(
    new_comment: ReviewComment,
    existing_comments: list[ReviewComment],
) -> bool:
    """
    Check if a comment is a duplicate of any existing comment.

    Duplicate means either:
    1. Same file + overlapping lines (location-based)
    2. Same file + similar message content (content-based)
    """
    for existing_comment in existing_comments:
        # Location-based overlap
        if _comments_overlap_by_lines(new_comment, existing_comment):
            return True

        # Content-based similarity (same file, different lines but same message)
        if new_comment.file == existing_comment.file:
            if _comments_similar_by_content(new_comment, existing_comment):
                return True

    return False


def _deduplicate(comments: list[ReviewComment], max_comments: int = 20) -> list[ReviewComment]:
    """
    Deduplicate overlapping or similar comments, preferring higher severity.
    Limits total comments to avoid noisy reviews.

    Deduplication checks:
    1. Location-based: same file + overlapping lines
    2. Content-based: same file + similar message keywords
    """
    severity_rank = {"error": 3, "warning": 2, "info": 1}

    sorted_comments = sorted(
        comments,
        key=lambda comment: (-severity_rank[comment.severity], comment.file, comment.line_start)
    )

    kept: list[ReviewComment] = []
    for comment in sorted_comments:
        if not _is_duplicate_comment(comment, kept):
            kept.append(comment)
        if len(kept) >= max_comments:
            break

    return kept


def _generate_summary(comments: list[ReviewComment], warnings: list[ReviewWarning]) -> str:
    """Generate a summary of the review, including any warnings about skipped content."""
    lines: list[str] = []

    # Add warnings section first if any
    if warnings:
        lines.append("**⚠️ Review Warnings:**")
        for warning in warnings:
            if warning.file:
                lines.append(f"- `{warning.file}`: {warning.message}")
            else:
                lines.append(f"- {warning.message}")
        lines.append("")

    if not comments:
        lines.append("No issues found. The code looks good.")
        return "\n".join(lines)

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

    lines.extend([
        f"**Risk Level:** {risk}",
        "",
        f"**Issues Found:** {len(comments)} total",
        f"- Errors: {error_count}",
        f"- Warnings: {warning_count}",
        f"- Info: {info_count}",
        "",
        "**By Category:**",
    ])
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

    Routes files to relevant agents, executes in parallel, deduplicates
    overlapping comments, and generates a summary.
    """
    # Route files to agents based on extension
    # Currently permissive - both agents see JS/TS files
    # TODO: Tighten routing as we learn agent strengths
    amethyst_files = _filter_files_for_agent(pr.files, AMETHYST_EXTENSIONS)
    citrine_files = _filter_files_for_agent(pr.files, CITRINE_EXTENSIONS)

    # Build filtered PR inputs for each agent
    amethyst_pr = PullRequestInput(
        number=pr.number,
        title=pr.title,
        files=amethyst_files,
        base_sha=pr.base_sha,
        head_sha=pr.head_sha,
    )
    citrine_pr = PullRequestInput(
        number=pr.number,
        title=pr.title,
        files=citrine_files,
        base_sha=pr.base_sha,
        head_sha=pr.head_sha,
    )

    # Run agents in parallel (only if they have files to review)
    tasks = []
    if amethyst_files:
        tasks.append(review_amethyst(amethyst_pr))
    if citrine_files:
        tasks.append(review_citrine(citrine_pr))

    if not tasks:
        # No reviewable files - return empty review
        return CouncilReview(
            comments=[],
            warnings=[],
            summary="No reviewable files found (no .ts, .tsx, .js, .jsx files in this PR).",
        )

    results = await asyncio.gather(*tasks)

    all_comments: list[ReviewComment] = []
    all_warnings: list[ReviewWarning] = []

    for agent_result in results:
        all_comments.extend(agent_result.comments)
        all_warnings.extend(agent_result.warnings)

    # Moderator sanity gate: enforce quality rules BEFORE deduplication
    # This ensures hedging errors become warnings and false positives are dropped
    sanitized_comments = _sanitize_comments(all_comments)

    final_comments = _deduplicate(sanitized_comments, max_comments=max_comments)
    summary = _generate_summary(final_comments, all_warnings)

    return CouncilReview(comments=final_comments, warnings=all_warnings, summary=summary)
