# QuartzCouncil

An opt-in, on-demand AI pull request reviewer that runs only when explicitly requested by a developer.

## What It Is

QuartzCouncil is a GitHub App backend that provides AI-powered code review using a multi-agent "review council" architecture. It is **not** an automatic reviewer — it never runs unless a developer explicitly triggers it.

## How It Works

### Triggering a Review

To request a review, comment on any pull request:

```
/quartz review
```

QuartzCouncil listens for `issue_comment.created` webhook events. When it detects this command on a PR, it runs the review pipeline. All other webhook events are ignored.

### Review Pipeline

```
Trigger → Fetch PR Diff → Specialist Agents (parallel) → Moderator → GitHub inline comments + summary
```

1. Developer comments `/quartz review` on a PR
2. QuartzCouncil fetches the PR files and diffs
3. Specialized reviewer agents analyze the code in parallel
4. The Quartz moderator merges, deduplicates, and summarizes feedback
5. Results are posted as inline PR comments + one summary comment

### Council Members

| Agent | Role | Focus Areas |
|-------|------|-------------|
| **Amethyst** | TypeScript Correctness | `any`/`unknown` misuse, unsafe casting, missing narrowing, generics, Zod schema drift |
| **Citrine** | React/Next.js Quality | Re-renders, effect lifecycle, memo misuse, event listener leaks, server/client boundaries, hook correctness |
| **Quartz** | Moderator | Deduplicates overlapping comments, normalizes severity, enforces comment limits, generates summary |

## Design Principles

- **Developer control** — Reviews run only when explicitly requested
- **High-signal, low-noise** — Specialized agents with narrow focus areas
- **Opt-in by default** — Webhook events are notifications, not triggers

## Development Setup

### 1. Install Dependencies

```bash
uv sync
```

### 2. Configure Environment

Create a `.env` file with:

```bash
OPENAI_API_KEY=sk-...
GITHUB_WEBHOOK_SECRET=your-webhook-secret
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=secrets/quartzcouncil.private-key.pem
```

### 3. Run the Server

```bash
uv run ./src/quartzcouncil/__main__.py
```

Server runs at `http://localhost:8000` with hot-reload enabled.

### 4. Expose Local Server (ngrok)

To receive GitHub webhooks locally:

```bash
# Terminal 1: Start the server
uv run ./src/quartzcouncil/__main__.py

# Terminal 2: Start ngrok tunnel
ngrok http 8000
```

Copy the ngrok HTTPS URL (e.g., `https://abc123.ngrok-free.app`) and set it as your GitHub App's webhook URL:

```
https://abc123.ngrok-free.app/github/webhook
```

Note: ngrok requires a free account. Run `ngrok config add-authtoken <token>` after signing up.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | Your OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for review agents |
| `OPENAI_TEMPERATURE` | `0.1` | Temperature for LLM calls |
| `GITHUB_WEBHOOK_SECRET` | (required) | Webhook secret from GitHub App settings |
| `GITHUB_APP_ID` | (required) | Your GitHub App ID |
| `GITHUB_PRIVATE_KEY_PATH` | `secrets/quartzcouncil.private-key.pem` | Path to GitHub App private key |

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

### Future Triggers

- **GitHub Check Run button** — Trigger reviews via GitHub's native Checks interface instead of a comment

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
