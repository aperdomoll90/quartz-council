from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

AgentName = Literal["Amethyst", "Citrine", "Chalcedony"]  # Future: Rutile, Smoky, Onyx, Agate, Phantom, Rose
Severity = Literal["info", "warning", "error"]
Category = Literal["types", "perf", "arch", "consistency", "ui", "a11y", "security", "ux"]


class RawComment(BaseModel):
    """LLM output — no agent field, injected later in code."""
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    severity: Severity
    category: Category
    message: str
    suggestion: Optional[str] = None


class ReviewComment(RawComment):
    """Final comment with agent metadata attached."""
    agent: AgentName


class ReviewWarning(BaseModel):
    """Warning about something that couldn't be fully reviewed."""
    kind: Literal["skipped_large_file", "batch_output_limit", "rate_limited"]
    message: str
    file: Optional[str] = None


class TokenUsage(BaseModel):
    """Token usage for a single LLM call."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    agent: Optional[str] = None
    batch_index: int = 0

    def cost_usd(self, model: str = "gpt-4o-mini") -> float:
        """Estimate cost in USD based on model pricing."""
        pricing = {
            "gpt-4o-mini": {"input": 0.15, "output": 0.60},
            "gpt-4o": {"input": 2.50, "output": 10.00},
            "gpt-4-turbo": {"input": 10.00, "output": 30.00},
        }
        rates = pricing.get(model, pricing["gpt-4o-mini"])
        input_cost = (self.input_tokens / 1_000_000) * rates["input"]
        output_cost = (self.output_tokens / 1_000_000) * rates["output"]
        return input_cost + output_cost


class ReviewMeta(BaseModel):
    """Metadata about the review request and execution."""
    triggered_by: Optional[str] = None  # GitHub username
    triggered_by_id: Optional[int] = None  # GitHub user ID
    token_usage: list[TokenUsage] = []

    @property
    def total_tokens(self) -> int:
        return sum(usage.total_tokens for usage in self.token_usage)

    @property
    def total_input_tokens(self) -> int:
        return sum(usage.input_tokens for usage in self.token_usage)

    @property
    def total_output_tokens(self) -> int:
        return sum(usage.output_tokens for usage in self.token_usage)

    def total_cost_usd(self, model: str = "gpt-4o-mini") -> float:
        return sum(usage.cost_usd(model) for usage in self.token_usage)
