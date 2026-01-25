"""
Shared prompt fragments for all QuartzCouncil reviewer agents.

These fragments ensure consistency in:
- General review rules and quality standards
- Line number accuracy instructions
- Forbidden hedging phrases
- Output format and limits
"""

# =============================================================================
# GENERAL RULES
# =============================================================================
# Core principles that apply to all reviewer agents.
# =============================================================================

GENERAL_RULES = """GENERAL RULES
- Only comment on code present in the diff.
- Prefer fewer comments; avoid repeating the same point.
- Every comment MUST point to a concrete, demonstrable issue in the code.
- info severity: NEVER use this - if it's not error/warning, don't report it.
- If you are not 95%+ confident the issue is real AND provable, DO NOT report it."""


# =============================================================================
# LINE NUMBER ACCURACY
# =============================================================================
# Critical instructions for accurate line references in comments.
# The diff is pre-processed to include L### prefixes for each line.
# =============================================================================

LINE_NUMBER_ACCURACY = """LINE NUMBER ACCURACY (CRITICAL)
- Each line in the diff is prefixed with its line number (e.g., "L 209 +export function...")
- line_start MUST match the L### prefix on the line with the issue
- Do NOT guess - read the L### prefix directly from the diff"""


# =============================================================================
# FORBIDDEN PHRASES
# =============================================================================
# Hedging language that indicates low-confidence or advice-style comments.
# These phrases should never appear in review comments.
# =============================================================================

FORBIDDEN_PHRASES = """FORBIDDEN PHRASES (never use these in your comments)
- "consider", "might want to", "could potentially", "may cause"
- "it would be better", "I suggest", "you should consider"
- "for better safety", "to be safe", "just in case"
- "would recommend", "generally speaking\""""


# =============================================================================
# OUTPUT RULES
# =============================================================================
# Constraints on the number and quality of comments returned.
# =============================================================================

OUTPUT_RULES = """OUTPUT
Return at most 5 ReviewComment objects. Keep only the highest-severity provable issues.
If no real issues, return an empty list. An empty list is a GOOD outcome - it means clean code."""


# =============================================================================
# COMBINED SHARED SECTION
# =============================================================================
# Pre-combined block for easy inclusion in agent prompts.
# =============================================================================

SHARED_RULES = f"""{GENERAL_RULES}

{LINE_NUMBER_ACCURACY}

{FORBIDDEN_PHRASES}

{OUTPUT_RULES}"""
