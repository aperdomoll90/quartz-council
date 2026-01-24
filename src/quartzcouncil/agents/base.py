from __future__ import annotations
import hashlib
import os

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from openai import LengthFinishReasonError
from pydantic import BaseModel

from quartzcouncil.core.types import RawComment, ReviewComment, ReviewWarning, AgentName
from quartzcouncil.core.pr_models import PullRequestInput, PullRequestFile


class AgentResult(BaseModel):
    """Result from an agent run, including comments and any warnings."""
    comments: list[ReviewComment]
    warnings: list[ReviewWarning]


# =============================================================================
# CHUNKING CONFIGURATION
# =============================================================================
# Large PRs are split into batches to avoid token limit errors.
# gpt-4o-mini has 16K output limit, so we keep batches small.
# =============================================================================

MAX_CHARS_PER_BATCH = 40_000  # Character budget per batch
MAX_FILES_PER_BATCH = 12      # Max files per batch
MAX_PATCH_SIZE = 60_000       # Skip patches larger than this (likely generated/minified)


class AgentOutput(BaseModel):
    """LLM returns raw comments without agent field."""
    comments: list[RawComment]


def build_diff(pr: PullRequestInput) -> str:
    """Format PR files into a readable diff string."""
    parts = []
    for pr_file in pr.files:
        parts.append(f"\n--- FILE: {pr_file.filename} ---\n{pr_file.patch}")
    return "\n".join(parts)


class ChunkResult(BaseModel):
    """Result of chunking files into batches."""
    batches: list[list[PullRequestFile]]
    skipped_files: list[str]


# =============================================================================
# FILE PRIORITIZATION
# =============================================================================
# When batches are capped, we want to review the most important files first.
# Priority order: components > hooks > pages > utils > tests > configs
# =============================================================================

def _get_file_priority(filepath: str) -> int:
    """
    Return priority score for a file (lower = higher priority).

    Priority order:
    0 - Components (likely user-facing, highest impact)
    1 - Hooks (shared logic, high impact)
    2 - Pages/routes (user-facing)
    3 - Utils/helpers (shared code)
    4 - Tests (important but lower risk)
    5 - Config/generated (lowest priority)
    """
    filepath_lower = filepath.lower()
    filename = filepath.rsplit('/', 1)[-1].lower() if '/' in filepath else filepath.lower()

    # Config and generated files - lowest priority
    if any(pattern in filepath_lower for pattern in [
        "config", ".config.", "generated", ".gen.", "mock", "__mock__",
        "package.json", "tsconfig", "eslint", "prettier", ".d.ts",
    ]):
        return 5

    # Test files
    if any(pattern in filepath_lower for pattern in [
        ".test.", ".spec.", "__tests__", "/tests/", "/test/",
    ]):
        return 4

    # Utils and helpers
    if any(pattern in filepath_lower for pattern in [
        "/utils/", "/util/", "/helpers/", "/helper/", "/lib/", "/services/",
        "utils.", "helper.", "service.",
    ]):
        return 3

    # Pages and routes
    if any(pattern in filepath_lower for pattern in [
        "/pages/", "/app/", "/routes/", "page.", "route.", "layout.",
    ]):
        return 2

    # Hooks
    if any(pattern in filepath_lower for pattern in [
        "/hooks/", "/hook/", "use",
    ]) and filename.startswith("use"):
        return 1

    # Components (default for .tsx files, or explicit component directories)
    if any(pattern in filepath_lower for pattern in [
        "/components/", "/component/", "component.",
    ]) or filepath_lower.endswith(".tsx"):
        return 0

    # Default: treat as utils-level
    return 3


def _get_file_sort_key(filepath: str) -> tuple[int, str, str]:
    """
    Sort key for files: priority first, then directory, then filename.
    This ensures high-priority files are batched first.
    """
    priority = _get_file_priority(filepath)
    if '/' in filepath:
        directory = filepath.rsplit('/', 1)[0]
    else:
        directory = ''
    return (priority, directory, filepath)


def chunk_files_by_char_budget(
    files: list[PullRequestFile],
    max_chars: int = MAX_CHARS_PER_BATCH,
    max_files: int = MAX_FILES_PER_BATCH,
) -> ChunkResult:
    """
    Split files into batches based on character budget.
    Skips gigantic patches (likely generated/minified).
    Returns both batches and list of skipped filenames.

    Files are sorted by directory then filename before batching to ensure
    deterministic batch composition and keep related files together.
    """
    batches: list[list[PullRequestFile]] = []
    current_batch: list[PullRequestFile] = []
    current_chars = 0
    skipped_files: list[str] = []

    # Sort by priority, then directory, then filename for deterministic batching
    # This ensures high-priority files (components, hooks) are reviewed first
    sorted_files = sorted(files, key=lambda pr_file: _get_file_sort_key(pr_file.filename))

    for pr_file in sorted_files:
        patch = pr_file.patch or ""
        patch_size = len(patch)

        # Skip gigantic patches (generated/minified files)
        if patch_size > MAX_PATCH_SIZE:
            print(f"[QuartzCouncil] ⏭️ Skipping large patch: {pr_file.filename} ({patch_size} chars)")
            skipped_files.append(pr_file.filename)
            continue

        # Start new batch if current would exceed limits
        if current_batch and (current_chars + patch_size > max_chars or len(current_batch) >= max_files):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0

        current_batch.append(pr_file)
        current_chars += patch_size

    # Don't forget the last batch
    if current_batch:
        batches.append(current_batch)

    return ChunkResult(batches=batches, skipped_files=skipped_files)


# =============================================================================
# HEDGING FILTER
# =============================================================================
# LLMs often hedge even when told not to. This filter catches common patterns.
# =============================================================================

HEDGING_PHRASES = [
    "consider ",
    "might want to",
    "could potentially",
    "may cause",
    "may lead to",
    "can lead to",
    "might lead to",
    "could lead to",
    "it would be better",
    "i suggest",
    "you should consider",
    "for better safety",
    "to be safe",
    "just in case",
    "potentially",
    "possibly",
    "arguably",
    "you might",
    "it might be",
    "could be improved",
    "would recommend",
    "best practice",
    "generally speaking",
]

# =============================================================================
# FALSE POSITIVE FILTERS
# =============================================================================
# These patterns catch common LLM mistakes that slip through prompt instructions.
# Each filter targets a specific false positive pattern identified in testing.
# =============================================================================

FALSE_POSITIVE_PATTERNS = [
    # Context typed as T | null is valid React pattern - not an error
    ("context", "null", "error"),
    # "infinite loop" claims without proof are almost always wrong
    ("infinite loop", None, "error"),
    ("infinite re-render", None, "error"),
    # "memory leak" claims require proof of missing cleanup
    ("memory leak", None, "error"),
    # setState in useEffect is not automatically an infinite loop
    ("setstate", "useeffect", "error"),
    ("set state", "useeffect", "error"),
    # "without checking" is speculation about missing guards
    ("without checking", None, "error"),
    # "can throw" / "can cause" is speculative
    ("can throw", None, "error"),
    ("can cause", None, "error"),
]


def _is_false_positive_error(comment: ReviewComment) -> bool:
    """
    Check if an ERROR-severity comment matches known false positive patterns.

    These are patterns where the LLM commonly claims ERROR but the issue is
    either not a bug or requires external context to determine.
    """
    if comment.severity != "error":
        return False

    message_lower = comment.message.lower()

    for pattern in FALSE_POSITIVE_PATTERNS:
        keyword1, keyword2, target_severity = pattern

        # Only filter if targeting this severity
        if target_severity != "error":
            continue

        # Check if pattern matches
        if keyword1 in message_lower:
            if keyword2 is None or keyword2 in message_lower:
                return True

    return False


def _is_hedging_comment(comment: ReviewComment) -> bool:
    """Check if a comment contains hedging language."""
    message_lower = comment.message.lower()
    suggestion_lower = (comment.suggestion or "").lower()
    combined = message_lower + " " + suggestion_lower

    for phrase in HEDGING_PHRASES:
        if phrase in combined:
            return True

    # Also filter info-level comments (should never be used per prompts)
    if comment.severity == "info":
        return True

    return False


def _filter_low_quality_comments(comments: list[ReviewComment]) -> list[ReviewComment]:
    """
    Remove comments that are hedging or match false positive patterns.

    Filters:
    1. Hedging language (speculative phrasing)
    2. False positive ERROR patterns (context|null, infinite loop claims, etc.)
    """
    filtered: list[ReviewComment] = []

    for comment in comments:
        if _is_hedging_comment(comment):
            continue
        if _is_false_positive_error(comment):
            continue
        filtered.append(comment)

    return filtered


def _compute_content_seed(diff_content: str) -> int:
    """
    Compute a deterministic seed from diff content.
    Same diff content always produces the same seed for reproducible LLM outputs.
    """
    content_hash = hashlib.sha256(diff_content.encode()).hexdigest()
    # Take first 8 hex chars and convert to int (max ~4 billion, fits in seed range)
    return int(content_hash[:8], 16)


async def run_review_agent(
    pr: PullRequestInput,
    agent_name: AgentName,
    prompt: ChatPromptTemplate,
) -> tuple[list[ReviewComment], bool]:
    """
    Shared execution logic for review agents (single batch).

    LLM outputs RawComment (no agent), we inject agent deterministically.
    Catches LengthFinishReasonError and returns ([], True) on failure.

    Uses a content-based seed for reproducibility: same diff content
    produces same LLM output (best effort, not guaranteed by OpenAI).

    Returns:
        Tuple of (comments, failed) where failed=True if output limit was hit.
    """
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0"))

    diff_content = build_diff(pr)
    content_seed = _compute_content_seed(diff_content)

    llm = ChatOpenAI(model=model, temperature=temperature, seed=content_seed)
    structured_llm = llm.with_structured_output(AgentOutput)

    chain = prompt | structured_llm

    try:
        result: AgentOutput = await chain.ainvoke({
            "diff": diff_content
        })
    except LengthFinishReasonError:
        print(f"[QuartzCouncil] ⚠️ {agent_name} hit output limit, batch failed")
        return ([], True)

    # Inject agent name in code — not from LLM
    comments = [
        ReviewComment(agent=agent_name, **raw.model_dump())
        for raw in result.comments
    ]

    # Filter out low-quality comments (hedging + false positive patterns)
    filtered_comments = _filter_low_quality_comments(comments)
    filtered_count = len(comments) - len(filtered_comments)
    if filtered_count > 0:
        print(f"[QuartzCouncil] 🔇 {agent_name} filtered {filtered_count} low-quality comments")

    print(f"[QuartzCouncil] 📊 {agent_name} returned {len(filtered_comments)} comments")
    return (filtered_comments, False)


MAX_BATCHES_PER_AGENT = 5  # Cap batches to limit API costs


async def run_review_agent_batched(
    pr: PullRequestInput,
    agent_name: AgentName,
    prompt: ChatPromptTemplate,
    max_chars: int = MAX_CHARS_PER_BATCH,
    max_files: int = MAX_FILES_PER_BATCH,
    max_batches: int = MAX_BATCHES_PER_AGENT,
) -> AgentResult:
    """
    Run review agent on large PRs by chunking into batches.

    Splits files by character budget, runs agent on each batch sequentially,
    and merges all comments. Deduplication happens later in Quartz moderator.
    Returns AgentResult with comments and any warnings about skipped content.
    """
    chunk_result = chunk_files_by_char_budget(pr.files, max_chars=max_chars, max_files=max_files)
    batches = chunk_result.batches
    warnings: list[ReviewWarning] = []

    # Add warnings for skipped large files
    for skipped_file in chunk_result.skipped_files:
        warnings.append(ReviewWarning(
            kind="skipped_large_file",
            message=f"File too large to review (>60K chars)",
            file=skipped_file,
        ))

    if not batches:
        return AgentResult(comments=[], warnings=warnings)

    # Cap batches to limit API costs
    if len(batches) > max_batches:
        skipped_batch_count = len(batches) - max_batches
        skipped_file_count = sum(len(batch) for batch in batches[max_batches:])
        print(f"[QuartzCouncil] ⚠️ {agent_name} capping at {max_batches} batches (skipping {skipped_batch_count} batches, {skipped_file_count} files)")
        warnings.append(ReviewWarning(
            kind="rate_limited",
            message=f"PR too large: reviewed first {max_batches} batches, skipped {skipped_file_count} files",
        ))
        batches = batches[:max_batches]

    if len(batches) > 1:
        print(f"[QuartzCouncil] 📦 {agent_name} processing {len(batches)} batches...")

    all_comments: list[ReviewComment] = []

    for batch_index, batch_files in enumerate(batches):
        batch_pr = PullRequestInput(
            number=pr.number,
            title=pr.title,
            files=batch_files,
            base_sha=pr.base_sha,
            head_sha=pr.head_sha,
        )

        if len(batches) > 1:
            print(f"[QuartzCouncil] 📦 {agent_name} batch {batch_index + 1}/{len(batches)} ({len(batch_files)} files)")

        comments, batch_failed = await run_review_agent(batch_pr, agent_name, prompt)
        all_comments.extend(comments)

        if batch_failed:
            file_names = [f.filename for f in batch_files]
            warnings.append(ReviewWarning(
                kind="batch_output_limit",
                message=f"Output limit hit, batch partially reviewed: {', '.join(file_names[:3])}{'...' if len(file_names) > 3 else ''}",
            ))

    return AgentResult(comments=all_comments, warnings=warnings)
