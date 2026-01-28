from __future__ import annotations
import asyncio
from pathlib import Path

from pydantic import BaseModel

from quartzcouncil.core.types import ReviewComment, ReviewWarning, ReviewMeta, TokenUsage
from quartzcouncil.core.pr_models import PullRequestInput, PullRequestFile
from quartzcouncil.core.config_models import QuartzCouncilConfig
from quartzcouncil.agents.amethyst import review_amethyst
from quartzcouncil.agents.citrine import review_citrine
from quartzcouncil.agents.chalcedony import review_chalcedony


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
    meta: ReviewMeta = ReviewMeta()


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
    similarity_threshold: float = 0.6,
) -> bool:
    """
    Return True if two comments have similar content based on keyword overlap.
    Uses Jaccard similarity on extracted keywords.

    Threshold of 0.6 (60%) requires substantial overlap to be considered duplicate.
    This prevents false deduplication of comments about different issues that
    happen to use similar language (e.g., "missing cleanup for X" vs "missing cleanup for Y").
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


def _merge_comment_messages(existing: ReviewComment, new_comment: ReviewComment) -> ReviewComment:
    """
    Merge two comments on the same location into one combined comment.
    Combines messages with bullet points, keeps highest severity, merges categories.
    """
    severity_rank = {"error": 3, "warning": 2, "info": 1}

    # Use highest severity
    merged_severity = existing.severity if severity_rank[existing.severity] >= severity_rank[new_comment.severity] else new_comment.severity

    # Combine messages with bullet points if not already bulleted
    existing_msg = existing.message.strip()
    new_msg = new_comment.message.strip()

    # If existing already has bullets, add new one
    if existing_msg.startswith("• ") or existing_msg.startswith("- "):
        merged_message = f"{existing_msg}\n• {new_msg}"
    else:
        merged_message = f"• {existing_msg}\n• {new_msg}"

    # Keep category from higher-severity comment (existing is processed first due to sorting)
    # Category is a Literal type so we can't combine them
    merged_category = existing.category

    return ReviewComment(
        file=existing.file,
        line_start=min(existing.line_start, new_comment.line_start),
        line_end=max(existing.line_end, new_comment.line_end),
        severity=merged_severity,
        category=merged_category,
        message=merged_message,
        suggestion=existing.suggestion or new_comment.suggestion,
        agent=existing.agent,
    )


def _deduplicate(
    comments: list[ReviewComment],
    max_comments: int = 20,
    content_similarity: bool = True,
    merge_overlapping: bool = False,
    debug: bool = False,
) -> list[ReviewComment]:
    """
    Deduplicate overlapping or similar comments, preferring higher severity.
    Limits total comments to avoid noisy reviews.

    Args:
        comments: List of comments to deduplicate
        max_comments: Maximum comments to keep. 0 means no limit (keep all unique).
        content_similarity: If True, also dedupe by message similarity. If False,
                           only dedupe by location (same file + overlapping lines).
        merge_overlapping: If True, merge overlapping comments into one combined comment
                          instead of dropping duplicates. Useful for Chalcedony.
        debug: If True, print debug info about what's being merged/dropped.

    Deduplication checks:
    1. Location-based: same file + overlapping lines (merge or drop based on merge_overlapping)
    2. Content-based: same file + similar message keywords (if content_similarity=True)
    """
    severity_rank = {"error": 3, "warning": 2, "info": 1}

    sorted_comments = sorted(
        comments,
        key=lambda comment: (-severity_rank[comment.severity], comment.file, comment.line_start)
    )

    kept: list[ReviewComment] = []
    if debug:
        print(f"[DEBUG] Starting deduplication with {len(sorted_comments)} comments")
        for idx, c in enumerate(sorted_comments):
            print(f"[DEBUG]   [{idx}] {c.file}:{c.line_start}-{c.line_end} ({c.severity})")

    for comment in sorted_comments:
        merged = False
        is_dup = False

        for idx, existing in enumerate(kept):
            # Check location overlap
            if _comments_overlap_by_lines(comment, existing):
                if debug:
                    print(f"[DEBUG] OVERLAP: {comment.file}:{comment.line_start}-{comment.line_end} overlaps with kept[{idx}] {existing.file}:{existing.line_start}-{existing.line_end}")
                if merge_overlapping:
                    # Merge instead of dropping
                    kept[idx] = _merge_comment_messages(existing, comment)
                    if debug:
                        print(f"[DEBUG]   -> Merged into kept[{idx}], now lines {kept[idx].line_start}-{kept[idx].line_end}")
                    merged = True
                else:
                    if debug:
                        print(f"[DEBUG]   -> Dropped (overlap, no merge)")
                    is_dup = True
                break

            # Only check content similarity if enabled (and not already merged)
            if content_similarity and comment.file == existing.file:
                if _comments_similar_by_content(comment, existing):
                    if debug:
                        print(f"[DEBUG] SIMILAR: {comment.file}:{comment.line_start} similar to kept[{idx}] {existing.file}:{existing.line_start}")
                    if merge_overlapping:
                        kept[idx] = _merge_comment_messages(existing, comment)
                        if debug:
                            print(f"[DEBUG]   -> Merged into kept[{idx}]")
                        merged = True
                    else:
                        if debug:
                            print(f"[DEBUG]   -> Dropped (similar, no merge)")
                        is_dup = True
                    break

        if not is_dup and not merged:
            kept.append(comment)
            if debug:
                print(f"[DEBUG] KEPT: {comment.file}:{comment.line_start}-{comment.line_end} as kept[{len(kept)-1}]")
        # max_comments=0 means no limit
        if max_comments > 0 and len(kept) >= max_comments:
            if debug:
                print(f"[DEBUG] Hit max_comments limit ({max_comments}), stopping")
            break

    if debug:
        print(f"[DEBUG] Final result: {len(kept)} comments")
        for idx, c in enumerate(kept):
            print(f"[DEBUG]   kept[{idx}]: {c.file}:{c.line_start}-{c.line_end}")

    return kept


def _format_usage_stats(meta: ReviewMeta) -> str:
    """Format usage statistics for the summary."""
    import os
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    cost = meta.total_cost_usd(model)

    parts = [
        f"**Usage:** {meta.total_tokens:,} tokens",
        f"(~${cost:.4f})",
    ]

    if meta.triggered_by:
        parts.append(f"| Triggered by @{meta.triggered_by}")

    return " ".join(parts)


def _generate_summary(
    comments: list[ReviewComment],
    warnings: list[ReviewWarning],
    meta: ReviewMeta | None = None,
) -> str:
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
        if meta and meta.total_tokens > 0:
            lines.append("")
            lines.append(_format_usage_stats(meta))
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

    # Add usage stats at the end
    if meta and meta.total_tokens > 0:
        lines.append("")
        lines.append(_format_usage_stats(meta))

    return "\n".join(lines)


async def review_council(
    pr: PullRequestInput,
    cfg: QuartzCouncilConfig | None = None,
    max_comments: int = 20,
    triggered_by: str | None = None,
    triggered_by_id: int | None = None,
) -> CouncilReview:
    """
    Run the Quartz council review.

    Routes files to relevant agents, executes in parallel, merges overlapping
    comments, and generates a summary.

    Chalcedony (repo conventions) is processed separately from Amethyst/Citrine:
    - Amethyst + Citrine: sanitized, merged (same-line issues combined), capped at max_comments
    - Chalcedony: merged separately, capped by config limits (default uncapped), appended after

    Comments on the same lines are merged into combined comments with bullet points,
    preserving all distinct issues while keeping the review focused.

    Args:
        pr: Pull request input with files and patches
        cfg: Optional repo config for Chalcedony agent (if None, Chalcedony is skipped)
        max_comments: Maximum distinct locations for Amethyst+Citrine (default 20)
        triggered_by: GitHub username of the person who triggered the review
        triggered_by_id: GitHub user ID of the person who triggered the review
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

    # ==========================================================================
    # RUN CORE AGENTS (Amethyst + Citrine) - bug/type detection
    # ==========================================================================
    core_tasks = []
    if amethyst_files:
        core_tasks.append(review_amethyst(amethyst_pr))
    if citrine_files:
        core_tasks.append(review_citrine(citrine_pr))

    # ==========================================================================
    # RUN CHALCEDONY SEPARATELY - repo conventions (if config exists)
    # ==========================================================================
    chalcedony_task = None
    chalcedony_max_comments = 0  # default: 0 = uncapped (report all violations)
    if cfg is not None and cfg.has_any_rules():
        chalcedony_task = review_chalcedony(pr, cfg)
        chalcedony_max_comments = cfg.limits.max_comments
        print(f"[QuartzCouncil] 🔧 Chalcedony max_comments from config: {chalcedony_max_comments}")

    # No agents to run at all
    if not core_tasks and chalcedony_task is None:
        return CouncilReview(
            comments=[],
            warnings=[],
            summary="No reviewable files found (no .ts, .tsx, .js, .jsx files in this PR).",
            meta=ReviewMeta(triggered_by=triggered_by, triggered_by_id=triggered_by_id),
        )

    # Run all agents in parallel
    all_tasks = core_tasks + ([chalcedony_task] if chalcedony_task else [])
    results = await asyncio.gather(*all_tasks)

    # Split results: core agents vs chalcedony
    core_results = results[:len(core_tasks)]
    chalcedony_result = results[len(core_tasks)] if chalcedony_task else None

    # ==========================================================================
    # PROCESS CORE COMMENTS (Amethyst + Citrine)
    # ==========================================================================
    core_comments: list[ReviewComment] = []
    all_warnings: list[ReviewWarning] = []

    for agent_result in core_results:
        core_comments.extend(agent_result.comments)
        all_warnings.extend(agent_result.warnings)

    # Moderator sanity gate for core agents
    # Merge overlapping comments (same location, different agents) into combined comments
    sanitized_core = _sanitize_comments(core_comments)
    final_core = _deduplicate(sanitized_core, max_comments=max_comments, merge_overlapping=True)

    # ==========================================================================
    # PROCESS CHALCEDONY COMMENTS (separately)
    # ==========================================================================
    final_chalcedony: list[ReviewComment] = []

    if chalcedony_result:
        all_warnings.extend(chalcedony_result.warnings)

        # Debug: show what files Chalcedony found violations in
        chalcedony_comments = chalcedony_result.comments
        files_with_violations = set(comment.file for comment in chalcedony_comments)
        print(f"[QuartzCouncil] 🔍 Chalcedony raw: {len(chalcedony_comments)} comments in {len(files_with_violations)} files")
        for idx, comment in enumerate(chalcedony_comments):
            line_range = f"L{comment.line_start}" if comment.line_start == comment.line_end else f"L{comment.line_start}-{comment.line_end}"
            print(f"[QuartzCouncil] 🔍   [{idx}] {comment.file}:{line_range} - {comment.message[:50]}...")

        # Chalcedony gets its own deduplication (within itself only)
        # No sanitization - repo rules are explicit, not speculative
        # Merge overlapping comments - multiple violations on same line become one combined comment
        # Disable content similarity - similar wording is expected for convention violations
        final_chalcedony = _deduplicate(
            chalcedony_comments,
            max_comments=chalcedony_max_comments,
            content_similarity=False,
            merge_overlapping=True,
            debug=False,
        )

        if final_chalcedony:
            print(f"[QuartzCouncil] 💎 Chalcedony: {len(final_chalcedony)} convention comments (separate from core)")

    # ==========================================================================
    # COMBINE: core comments first, then chalcedony appended
    # ==========================================================================
    final_comments = final_core + final_chalcedony

    # ==========================================================================
    # AGGREGATE TOKEN USAGE from all agents
    # ==========================================================================
    all_token_usage: list[TokenUsage] = []
    for agent_result in core_results:
        all_token_usage.extend(agent_result.token_usage)
    if chalcedony_result:
        all_token_usage.extend(chalcedony_result.token_usage)

    meta = ReviewMeta(
        triggered_by=triggered_by,
        triggered_by_id=triggered_by_id,
        token_usage=all_token_usage,
    )

    summary = _generate_summary(final_comments, all_warnings, meta)

    return CouncilReview(comments=final_comments, warnings=all_warnings, summary=summary, meta=meta)
