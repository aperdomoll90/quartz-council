from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

AgentName = Literal["Amethyst", "Citrine"]
Severity = Literal["info", "warning", "error"]
Category = Literal["types", "perf", "arch", "consistency", "ui"]

class ReviewComment(BaseModel):
    agent: AgentName
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    severity: Severity
    category: Category
    message: str
    suggestion: Optional[str] = None
