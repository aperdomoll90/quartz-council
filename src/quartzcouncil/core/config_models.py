"""
Configuration models for .quartzcouncil.yml repo config files.

These models define the structure for repo-specific rules that
the Chalcedony agent enforces during PR reviews.

Security considerations:
- All string fields have length limits to prevent token exhaustion
- Policy text is sanitized to prevent prompt injection
- List fields have max item limits
"""
from __future__ import annotations

import re
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


# =============================================================================
# SECURITY LIMITS
# =============================================================================
# These limits prevent abuse via oversized configs or prompt injection.
# =============================================================================

MAX_SHORT_STRING = 50       # For prefixes, separators, IDs
MAX_POLICY_TEXT = 500       # For freeform policy descriptions
MAX_POLICIES = 10           # Max number of policy rules
MAX_LIST_ITEMS = 20         # Max items in lists (e.g., allowed_prefixes)


def _sanitize_for_prompt(text: str) -> str:
    """
    Sanitize text that will be embedded in LLM prompts.

    Removes or escapes patterns that could be used for prompt injection:
    - Control characters
    - Common injection patterns (IGNORE, FORGET, SYSTEM, etc.)
    - Excessive whitespace/newlines
    """
    # Remove control characters except newline and tab
    text = "".join(char for char in text if char.isprintable() or char in "\n\t")

    # Collapse multiple newlines/spaces
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {3,}", " ", text)

    # Remove common prompt injection patterns (case-insensitive)
    # These patterns at the START of text are suspicious
    injection_patterns = [
        r"^ignore\s+(all\s+)?(previous\s+)?instructions?",
        r"^forget\s+(all\s+)?(previous\s+)?",
        r"^disregard\s+(all\s+)?(previous\s+)?",
        r"^override\s+",
        r"^system\s*:",
        r"^assistant\s*:",
        r"^user\s*:",
        r"^<\s*system\s*>",
        r"^###\s*(system|instruction)",
    ]

    text_lower = text.lower().strip()
    for pattern in injection_patterns:
        if re.match(pattern, text_lower):
            # Replace the suspicious text with a warning
            return "[BLOCKED: suspicious content removed]"

    return text.strip()


# =============================================================================
# RULE TOGGLE MODELS
# =============================================================================
# Structured rules with specific options. These are well-known patterns
# that can be enabled/disabled with configuration.
# =============================================================================


class BemNamingRule(BaseModel):
    """BEM naming convention enforcement."""
    enabled: bool = False
    prefix: str = Field(default="c-", max_length=MAX_SHORT_STRING)
    element_separator: str = Field(default="__", max_length=MAX_SHORT_STRING)
    modifier_separator: str = Field(default="--", max_length=MAX_SHORT_STRING)
    severity: Literal["warning", "error"] = "warning"


class ScssNestingRule(BaseModel):
    """SCSS nesting convention enforcement."""
    enabled: bool = False
    require_ampersand: bool = True
    severity: Literal["warning", "error"] = "warning"


class CssModulesAccessRule(BaseModel):
    """CSS Modules access pattern enforcement."""
    enabled: bool = False
    style_object: str = Field(default="styles", max_length=MAX_SHORT_STRING)
    bracket_notation_only: bool = True
    severity: Literal["warning", "error"] = "warning"


class DataAttributesRule(BaseModel):
    """Data attribute naming convention enforcement."""
    enabled: bool = False
    allowed_prefixes: list[str] = Field(default_factory=lambda: ["data-state", "data-variant"])
    severity: Literal["warning", "error"] = "warning"

    @field_validator("allowed_prefixes")
    @classmethod
    def validate_prefixes(cls, prefixes: list[str]) -> list[str]:
        """Limit number and length of prefixes."""
        if len(prefixes) > MAX_LIST_ITEMS:
            prefixes = prefixes[:MAX_LIST_ITEMS]
        return [prefix[:MAX_SHORT_STRING] for prefix in prefixes]


class ExtractUtilsRule(BaseModel):
    """Duplicate code extraction enforcement."""
    enabled: bool = False
    min_duplicates: int = Field(default=2, ge=1, le=10)
    severity: Literal["warning", "error"] = "warning"


class RuleToggles(BaseModel):
    """Container for all structured rule toggles."""
    bem_naming: Optional[BemNamingRule] = None
    scss_nesting: Optional[ScssNestingRule] = None
    css_modules_access: Optional[CssModulesAccessRule] = None
    data_attributes: Optional[DataAttributesRule] = None
    extract_utils: Optional[ExtractUtilsRule] = None


# =============================================================================
# POLICY MODELS
# =============================================================================
# Freeform textual rules for custom conventions not covered by toggles.
# =============================================================================


class PolicyRule(BaseModel):
    """A freeform policy rule defined in text."""
    id: str = Field(max_length=MAX_SHORT_STRING)
    severity: Literal["warning", "error"] = "warning"
    text: str = Field(max_length=MAX_POLICY_TEXT)

    @field_validator("text")
    @classmethod
    def sanitize_text(cls, text: str) -> str:
        """Sanitize policy text to prevent prompt injection."""
        return _sanitize_for_prompt(text)

    @field_validator("id")
    @classmethod
    def sanitize_id(cls, policy_id: str) -> str:
        """Sanitize policy ID - alphanumeric and hyphens only."""
        # Only allow alphanumeric, hyphens, underscores
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", policy_id)
        return sanitized[:MAX_SHORT_STRING] if sanitized else "unnamed"


# =============================================================================
# LIMITS
# =============================================================================


class Limits(BaseModel):
    """Review limits configuration."""
    # max_comments: 0 means no limit (Chalcedony reports all violations)
    # Default is 0 (uncapped) since convention comments shouldn't be limited
    max_comments: int = Field(default=0, ge=0, le=100)
    default_severity: Literal["warning", "error"] = "warning"


# =============================================================================
# AGENT TOGGLES
# =============================================================================


class AgentToggles(BaseModel):
    """Enable/disable individual review agents."""
    amethyst: bool = True   # TypeScript type safety
    citrine: bool = True    # React/Next.js performance
    chalcedony: bool = True # Repo-specific conventions (requires rules to be defined)


# =============================================================================
# ROOT CONFIG
# =============================================================================


class QuartzCouncilConfig(BaseModel):
    """
    Root configuration model for .quartzcouncil.yml.

    Example YAML:

    version: 1

    # Agent toggles - enable/disable specific reviewers
    agents:
      amethyst: true   # TypeScript type safety
      citrine: true    # React/Next.js performance
      chalcedony: true # Repo conventions (requires rules below)

    limits:
      max_comments: 5
      default_severity: warning

    rules:
      bem_naming:
        enabled: true
        prefix: "c-"
        element_separator: "__"
        modifier_separator: "--"
        severity: warning

      scss_nesting:
        enabled: true
        require_ampersand: true
        severity: warning

    policy:
      - id: "hooks-naming"
        severity: warning
        text: "Custom hooks must be named useX and must not be exported as default."
    """
    version: int = Field(default=1, ge=1, le=10)
    limits: Limits = Field(default_factory=Limits)
    rules: RuleToggles = Field(default_factory=RuleToggles)
    policy: list[PolicyRule] = Field(default_factory=list)
    agents: AgentToggles = Field(default_factory=AgentToggles)

    @field_validator("policy")
    @classmethod
    def limit_policies(cls, policies: list[PolicyRule]) -> list[PolicyRule]:
        """Limit number of policy rules to prevent abuse."""
        if len(policies) > MAX_POLICIES:
            print(f"[QuartzCouncil] ⚠️ Too many policies ({len(policies)}), truncating to {MAX_POLICIES}")
            return policies[:MAX_POLICIES]
        return policies

    def has_any_rules(self) -> bool:
        """Check if any rules or policies are enabled."""
        rules_dict = self.rules.model_dump(exclude_none=True)

        # Check if any structured rule is enabled
        for rule_name, rule_config in rules_dict.items():
            if isinstance(rule_config, dict) and rule_config.get("enabled", False):
                return True

        # Check if any policy rules exist
        if self.policy:
            return True

        return False
