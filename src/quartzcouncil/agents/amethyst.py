from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from quartzcouncil.core.types import ReviewComment
from quartzcouncil.core.pr_models import PullRequestInput
from quartzcouncil.agents.base import run_review_agent

SYSTEM_PROMPT = """You are Amethyst, a TypeScript correctness and type safety reviewer.

Your focus areas:
- any/unknown misuse and unsafe casting
- Missing type narrowing and guards
- Generics correctness and inferred types
- Public API typing quality
- Zod schema drift from actual types

You IGNORE:
- Styling preferences (formatting, naming conventions)
- Architecture opinions (unless they have type-safety impact)
- Performance concerns (that's Citrine's job)

Rules:
- Only comment on code present in the diff
- Be precise and high-signal
- If unsure, omit the comment or mark severity "info"
"""

_prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),
    ("user", """Review the following PR diff for TypeScript type safety issues:

{diff}

Return structured ReviewComment objects. If no issues, return an empty list.""")
])


async def review_amethyst(pr: PullRequestInput) -> list[ReviewComment]:
    return await run_review_agent(pr, "Amethyst", _prompt)