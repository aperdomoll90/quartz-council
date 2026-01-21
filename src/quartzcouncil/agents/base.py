from __future__ import annotations
import os

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from quartzcouncil.core.types import RawComment, ReviewComment, AgentName
from quartzcouncil.core.pr_models import PullRequestInput


class AgentOutput(BaseModel):
    """LLM returns raw comments without agent field."""
    comments: list[RawComment]


def build_diff(pr: PullRequestInput) -> str:
    """Format PR files into a readable diff string."""
    parts = []
    for pr_file in pr.files:
        parts.append(f"\n--- FILE: {pr_file.filename} ---\n{pr_file.patch}")
    return "\n".join(parts)


async def run_review_agent(
    pr: PullRequestInput,
    agent_name: AgentName,
    prompt: ChatPromptTemplate,
) -> list[ReviewComment]:
    """
    Shared execution logic for review agents.

    LLM outputs RawComment (no agent), we inject agent deterministically.
    """
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))

    llm = ChatOpenAI(model=model, temperature=temperature)
    structured_llm = llm.with_structured_output(AgentOutput)

    chain = prompt | structured_llm

    result: AgentOutput = await chain.ainvoke({
        "diff": build_diff(pr)
    })

    # Inject agent name in code — not from LLM
    return [
        ReviewComment(agent=agent_name, **raw.model_dump())
        for raw in result.comments
    ]
