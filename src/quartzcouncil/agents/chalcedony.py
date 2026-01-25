"""
Chalcedony - Repo-specific conventions and rules reviewer.

This agent ONLY enforces rules explicitly defined in .quartzcouncil.yml.
It does NOT invent rules or apply generic style preferences.
"""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from quartzcouncil.core.pr_models import PullRequestInput
from quartzcouncil.core.config_models import QuartzCouncilConfig
from quartzcouncil.agents.base import run_review_agent_batched, AgentResult
from quartzcouncil.prompts.shared import SHARED_RULES


# =============================================================================
# RULES CONTEXT BUILDER
# =============================================================================
# Converts QuartzCouncilConfig into a compact, token-efficient text block
# that gets embedded in the system prompt.
# =============================================================================


def build_rules_context(cfg: QuartzCouncilConfig) -> str:
    """
    Build a compact rules context string from config for embedding in prompt.

    Output format:
    REPO RULES (authoritative)
    - bem_naming: enabled, prefix=c-, separators __/--, severity=warning
    - POLICY hooks-naming (warning): Custom hooks must be named useX...
    """
    lines: list[str] = ["REPO RULES (authoritative — only enforce these, nothing else)"]

    rules_dict = cfg.rules.model_dump(exclude_none=True)

    # Structured rules
    if rules_dict.get("bem_naming", {}).get("enabled"):
        rule = cfg.rules.bem_naming
        lines.append(
            f"- bem_naming: enabled, prefix={rule.prefix}, "
            f"element_sep={rule.element_separator}, modifier_sep={rule.modifier_separator}, "
            f"severity={rule.severity}"
        )

    if rules_dict.get("scss_nesting", {}).get("enabled"):
        rule = cfg.rules.scss_nesting
        lines.append(
            f"- scss_nesting: enabled, require_ampersand={rule.require_ampersand}, "
            f"severity={rule.severity}"
        )

    if rules_dict.get("css_modules_access", {}).get("enabled"):
        rule = cfg.rules.css_modules_access
        lines.append(
            f"- css_modules_access: enabled, style_object={rule.style_object}, "
            f"bracket_only={rule.bracket_notation_only}, severity={rule.severity}"
        )

    if rules_dict.get("data_attributes", {}).get("enabled"):
        rule = cfg.rules.data_attributes
        prefixes = ", ".join(rule.allowed_prefixes)
        lines.append(
            f"- data_attributes: enabled, allowed_prefixes=[{prefixes}], "
            f"severity={rule.severity}"
        )

    if rules_dict.get("extract_utils", {}).get("enabled"):
        rule = cfg.rules.extract_utils
        lines.append(
            f"- extract_utils: enabled, min_duplicates={rule.min_duplicates}, "
            f"severity={rule.severity}"
        )

    # Policy rules (freeform)
    for policy in cfg.policy:
        lines.append(f"- POLICY {policy.id} ({policy.severity}): {policy.text}")

    # Limits
    lines.append("")
    lines.append(f"LIMITS: max_comments={cfg.limits.max_comments}, default_severity={cfg.limits.default_severity}")

    return "\n".join(lines)


# =============================================================================
# SYSTEM PROMPT
# =============================================================================


def _build_system_prompt(rules_context: str) -> str:
    """Build the full system prompt with embedded rules context."""
    return f"""You are Chalcedony, a repo-specific conventions and consistency reviewer.

MISSION
Enforce ONLY the rules defined below. You are NOT a generic code reviewer.
You must NOT invent rules, suggest improvements, or comment on anything not explicitly covered by the repo rules.

{rules_context}

CRITICAL CONSTRAINTS
- You can ONLY report violations of rules listed above
- If a pattern is not covered by any rule above, DO NOT comment on it
- Do NOT suggest "improvements" or "better practices" unless they are explicitly stated in the rules
- An empty comment list is the correct output if no rule violations are found

SEVERITY MAPPING
- Use the severity specified for each rule
- If a rule doesn't specify severity, use the default_severity from LIMITS
- Never emit "info" severity

CATEGORY
- Use "consistency" for BEM, naming, and pattern enforcement
- Use "ui" for CSS/SCSS/styling-related rules
- Use "arch" for code organization rules (like extract_utils)

{SHARED_RULES}"""


def _build_prompt(rules_context: str) -> ChatPromptTemplate:
    """Build the ChatPromptTemplate with embedded rules context."""
    return ChatPromptTemplate.from_messages([
        ("system", _build_system_prompt(rules_context)),
        ("user", """Review the following PR diff for violations of the repo-specific rules defined above:

{diff}

IMPORTANT: Only report violations of rules explicitly defined in REPO RULES.
If no violations are found, return an empty list.

Return structured ReviewComment objects. If no issues, return an empty list.""")
    ])


# =============================================================================
# AGENT ENTRY POINT
# =============================================================================


async def review_chalcedony(
    pr: PullRequestInput,
    cfg: QuartzCouncilConfig | None,
) -> AgentResult:
    """
    Run the Chalcedony agent to enforce repo-specific rules.

    Args:
        pr: The pull request input with files and patches
        cfg: The QuartzCouncilConfig loaded from repo, or None if not found

    Returns:
        AgentResult with comments and warnings. Returns empty if no config
        or no rules are enabled.
    """
    # No config = no rules to enforce
    if cfg is None:
        return AgentResult(comments=[], warnings=[])

    # No rules enabled = nothing to check
    if not cfg.has_any_rules():
        return AgentResult(comments=[], warnings=[])

    # Build rules context and prompt
    rules_context = build_rules_context(cfg)
    prompt = _build_prompt(rules_context)

    print(f"[QuartzCouncil] 💎 Chalcedony running with repo config")

    return await run_review_agent_batched(pr, "Chalcedony", prompt)
