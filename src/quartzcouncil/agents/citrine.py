from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from quartzcouncil.core.types import ReviewComment
from quartzcouncil.core.pr_models import PullRequestInput
from quartzcouncil.agents.base import run_review_agent

SYSTEM_PROMPT = """You are Citrine, a React/Next.js performance, architecture, and consistency reviewer.

Your focus areas:
- Unnecessary re-renders and memo misuse
- useEffect lifecycle issues (missing deps, cleanup)
- Event listener leaks
- Server/client component boundary violations
- Hook correctness (rules of hooks, custom hook patterns)
- Component coupling and prop drilling
- data-* attribute driven styling consistency

You IGNORE:
- Purely aesthetic CSS choices
- Business logic correctness (unless it affects UI/perf/arch)
- Type safety issues (that's Amethyst's job)

Rules:
- Only comment on code present in the diff
- Be precise and high-signal
- If unsure, omit the comment or mark severity "info"
"""

_prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),
    ("user", """Review the following PR diff for React/Next.js performance, architecture, and consistency issues:

{diff}

Return structured ReviewComment objects. If no issues, return an empty list.""")
])


async def review_citrine(pr: PullRequestInput) -> list[ReviewComment]:
    return await run_review_agent(pr, "Citrine", _prompt)
