# QuartzCouncil

A GitHub App that reviews pull requests using a multi-agent "review council" architecture.

## How It Works

Two specialized reviewer agents run in parallel, then a Moderator merges, deduplicates, and posts the final feedback as inline GitHub PR review comments.

### Council Members

| Agent | Role | Focus Areas |
|-------|------|-------------|
| **Amethyst** | TypeScript Correctness | `any`/`unknown` misuse, unsafe casting, missing narrowing, generics, Zod schema drift |
| **Citrine** | React/Next.js Quality | Re-renders, effect lifecycle, memo misuse, event listener leaks, server/client boundaries, hook correctness |
| **Quartz** | Moderator | Deduplicates overlapping comments, normalizes severity, enforces comment limits, generates summary |

## Setup

```bash
# Install dependencies
uv sync

# Set environment variables
cp .env.example .env
# Edit .env with your OPENAI_API_KEY

# Run the server
uv run ./src/quartzcouncil/__main__.py
```

Server runs at `http://localhost:8000` with hot-reload enabled.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | Your OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for review agents |
| `OPENAI_TEMPERATURE` | `0.1` | Temperature for LLM calls |

## Project Structure

```
src/quartzcouncil/
├── agents/
│   ├── base.py       # Shared agent execution logic
│   ├── amethyst.py   # TypeScript reviewer
│   ├── citrine.py    # React/Next.js reviewer
│   └── quartz.py     # Moderator (parallel exec, dedupe, summary)
├── core/
│   ├── types.py      # RawComment, ReviewComment, type aliases
│   └── pr_models.py  # PullRequestInput, PullRequestFile
└── github/
    └── webhooks/
        └── app.py    # FastAPI webhook handlers
```

## Tech Stack

- Python 3.12+ with UV
- LangChain + OpenAI for agent orchestration
- FastAPI for webhooks
- Pydantic for structured output validation

## Roadmap

### Planned Council Members

| Agent | Domain | Category |
|-------|--------|----------|
| **Rutile** | Critical-path & interaction performance (hot paths, animation/jank) | `perf` |
| **Smoky** | Accessibility (keyboard, focus, ARIA, reduced-motion) | `a11y` |
| **Onyx** | Node/Next server & security (validation, auth leaks, env exposure) | `security` |
| **Chalcedony** | Consistency & patterns (design system usage, API shape cohesion) | `consistency` |
| **Agate** | Architecture (boundaries, ownership, coupling) | `arch` |
| **Phantom** | Refactors & legacy risk (state evolution, regression traps) | `arch` |
| **Rose** | UX heuristics (interaction clarity, comfort, motion restraint) | `ux` |

### Pipeline Vision

```
PR Diff → File Router → Specialist Agents (parallel) → Moderator → GitHub inline comments + summary
```

Future versions will route diffs by file type to only relevant agents, reducing noise and cost.
