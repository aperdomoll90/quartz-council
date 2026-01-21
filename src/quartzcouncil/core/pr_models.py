from __future__ import annotations
from pydantic import BaseModel
from typing import Optional


class PullRequestFile(BaseModel):
    filename: str
    patch: str  # Unified diff (what GitHub gives you)


class PullRequestInput(BaseModel):
    """
    Normalized pull request input for QuartzCouncil agents.
    This is NOT GitHub-specific.
    """
    number: int
    title: str
    files: list[PullRequestFile]

    # Optional but useful later
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
