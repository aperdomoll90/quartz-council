# QuartzCouncil

An opt-in, on-demand AI pull request reviewer that runs only when explicitly requested by a developer.

## What It Is

QuartzCouncil is a GitHub App backend that provides AI-powered code review using a multi-agent "review council" architecture. It is **not** an automatic reviewer — it never runs unless a developer explicitly triggers it.

## Architecture

**Type:** Stateless webhook server (event-driven microservice)

```
GitHub → Webhook POST → FastAPI → Agents (parallel) → GitHub API
```

| Component | Technology | Purpose |
|-----------|------------|---------|
| Web framework | FastAPI + uvicorn | Async HTTP server for webhooks |
| LLM orchestration | LangChain + OpenAI | Structured output from GPT models |
| HTTP client | httpx | Async calls to GitHub/OpenAI APIs |
| Validation | Pydantic | Request/response schemas, LLM output parsing |
| Auth | PyJWT | GitHub App JWT + installation tokens |

**Stateless design:**
- No database (rate limiting is in-memory, resets on restart)
- No user sessions (GitHub App handles auth)
- Single process (use Redis for shared state if scaling horizontally)

## How It Works

### Triggering a Review

To request a review, comment on any pull request:

```
/quartz review
```

QuartzCouncil listens for `issue_comment.created` webhook events. When it detects this command on a PR, it runs the review pipeline. All other webhook events are ignored.

### Review Pipeline

```
Trigger → Rate Check → Idempotency Check → Fetch Diff → Batch & Route → Agents → Sanitize → Dedupe → Publish
```

1. Developer comments `/quartz review` on a PR
2. Rate limit check (5 reviews/hour/installation)
3. Idempotency check (skip if commit already reviewed, link to existing)
4. QuartzCouncil fetches the PR files and diffs
5. Files are sorted by priority and batched by size
6. Specialized reviewer agents analyze batches in parallel
7. Agent-level filters remove hedging and false positive patterns
8. Moderator sanity gate downgrades speculative ERRORs, drops known false positives
9. Content-based deduplication removes similar comments
10. Comments are snapped to nearest valid diff line for inline posting
11. Results are posted as inline PR comments + one summary comment

### Council Members

| Agent | Role | Focus Areas |
|-------|------|-------------|
| **Amethyst** | TypeScript Correctness | `any`/`unknown` misuse, unsafe casting, missing narrowing, generics, Zod schema drift |
| **Citrine** | React/Next.js Quality | Re-renders, effect lifecycle, memo misuse, event listener leaks, server/client boundaries, hook correctness |
| **Chalcedony** | Repo Conventions | Enforces rules from `.quartzcouncil.yml` — BEM naming, SCSS nesting, CSS Modules, data-* attributes, custom policies |
| **Quartz** | Moderator | Deduplicates comments, sanitizes false positives, normalizes severity, enforces limits, generates summary |

## Design Principles

- **Developer control** — Reviews run only when explicitly requested
- **High-signal, low-noise** — Specialized agents with narrow focus areas
- **Opt-in by default** — Webhook events are notifications, not triggers
- **Evidence required** — ERROR severity requires proof in the diff, not speculation
- **Idempotent** — Same commit won't be reviewed twice (links to existing review)

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

## Production Deployment (AWS Lambda)

QuartzCouncil can be deployed to AWS Lambda for production use.

### Architecture

```
GitHub → API Gateway → Receiver Lambda → SQS → Worker Lambda → GitHub API
```

- **Receiver Lambda**: Verifies webhook, enqueues job, returns 200 immediately
- **Worker Lambda**: Processes review (30-60s), posts to GitHub
- **SQS**: Decouples webhook response from processing
- **DynamoDB**: Idempotency store (prevents duplicate reviews)

### Deploy

```bash
cd infra/sam

# Prepare and build
./build.sh
sam build

# Deploy (first time)
sam deploy --guided

# Set secrets
aws secretsmanager put-secret-value --region us-east-1 \
  --secret-id quartzcouncil/openai_api_key --secret-string "sk-..."
aws secretsmanager put-secret-value --region us-east-1 \
  --secret-id quartzcouncil/github_webhook_secret --secret-string "..."
aws secretsmanager put-secret-value --region us-east-1 \
  --secret-id quartzcouncil/github_app_id --secret-string "123456"
aws secretsmanager put-secret-value --region us-east-1 \
  --secret-id quartzcouncil/github_private_key_pem --secret-string "$(cat key.pem)"
```

Update your GitHub App webhook URL to the API Gateway endpoint from the deploy output.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | Your OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for review agents |
| `OPENAI_TEMPERATURE` | `0` | Temperature for LLM calls (0 = deterministic) |
| `GITHUB_WEBHOOK_SECRET` | (required) | Webhook secret from GitHub App settings |
| `GITHUB_APP_ID` | (required) | Your GitHub App ID |
| `GITHUB_PRIVATE_KEY_PATH` | `secrets/quartzcouncil.private-key.pem` | Path to GitHub App private key |
| `QUARTZ_IDEMPOTENCY_CHECK` | `true` | Set to `false` to allow multiple reviews per commit (for testing) |

## Project Structure

```
src/quartzcouncil/
├── agents/
│   ├── base.py        # Batched runner, chunking, AgentResult
│   ├── amethyst.py    # TypeScript reviewer
│   ├── citrine.py     # React/Next.js reviewer
│   ├── chalcedony.py  # Repo conventions reviewer
│   └── quartz.py      # Moderator (parallel exec, dedupe, summary)
├── core/
│   ├── types.py         # RawComment, ReviewComment, ReviewWarning, TokenUsage, ReviewMeta
│   ├── pr_models.py     # PullRequestInput, PullRequestFile
│   ├── config_models.py # QuartzCouncilConfig, RuleToggles, PolicyRule
│   └── rate_limit.py    # In-memory rate limiter
├── prompts/
│   └── shared.py        # Shared prompt fragments
└── github/
    ├── auth.py       # JWT + installation token exchange
    ├── pr.py         # Fetch PR files
    ├── webhooks/
    │   └── app.py    # FastAPI webhook handlers
    └── client/
        ├── github_client.py    # HTTP client wrapper
        ├── pr_api.py           # PR metadata fetching
        ├── config_api.py       # Fetch .quartzcouncil.yml
        ├── review_publisher.py # Post reviews to GitHub
        └── diff_parser.py      # Parse patch hunks for line validation
```

## Tech Stack

- Python 3.12+ with UV
- LangChain + OpenAI for agent orchestration
- FastAPI for webhooks
- Pydantic for structured output validation
- httpx for async HTTP requests

## Limits & Cost Controls

| Control | Value | Purpose |
|---------|-------|---------|
| Rate limit | 5 reviews/hour/installation | Prevent spam/abuse |
| Max batches | 5 per agent (10 total) | Cap API costs |
| Max file size | 60K chars | Skip generated/minified files |
| Batch size | ~40K chars, 12 files | Stay within token limits |
| Max inline comments | 20 | Avoid noisy reviews |
| Per-batch comments | 5 max | Reduce noise per batch |
| Line snapping | Enabled | Snap comments to nearest valid diff line |

**Approximate costs (gpt-4o-mini):**
- Small PR (10 files): ~$0.01
- Medium PR (50 files): ~$0.05
- Huge PR (200 files): ~$0.05 (capped at 5 batches)

**Token tracking:** Each review reports total tokens used and estimated cost in the summary, along with who triggered the review (`@username`).

## Security Controls

QuartzCouncil implements defense-in-depth against malicious input:

### Webhook Security
| Control | Purpose |
|---------|---------|
| HMAC-SHA256 signature | Verify webhooks come from GitHub |
| Installation token scope | Tokens limited to specific repo |

### Config Injection Protection (`.quartzcouncil.yml`)

Repo owners can define custom rules in `.quartzcouncil.yml`. Since policy text is embedded in LLM prompts, we protect against prompt injection:

| Control | Value | Purpose |
|---------|-------|---------|
| `MAX_SHORT_STRING` | 50 chars | Limit IDs, prefixes, separators |
| `MAX_POLICY_TEXT` | 500 chars | Limit freeform policy descriptions |
| `MAX_POLICIES` | 10 | Cap number of policy rules |
| `MAX_LIST_ITEMS` | 20 | Cap list fields (allowed_prefixes, etc.) |

**Blocked injection patterns** (at start of policy text):
- `ignore previous instructions`, `forget all`, `disregard`
- `override`, `system:`, `assistant:`, `user:`
- `<system>`, `### system`, `### instruction`

**Additional sanitization:**
- Control characters stripped (except `\n`, `\t`)
- Excessive whitespace collapsed
- Policy IDs restricted to alphanumeric, hyphens, underscores
- YAML parsed with `safe_load` (no arbitrary Python execution)

## Quality Controls

QuartzCouncil uses multiple layers to reduce false positives:

| Layer | Location | What It Does |
|-------|----------|--------------|
| Strict prompts | Agent prompts | 95% confidence required, forbidden phrases |
| Hedging filter | `base.py` | Drops comments with speculative language |
| False positive filter | `base.py` | Blocks known bad patterns (infinite loop, memory leak) |
| Moderator sanity gate | `quartz.py` | Downgrades hedging ERRORs to WARNING |
| Known false positives | `quartz.py` | Drops factually wrong claims (next/image server-only) |
| Content deduplication | `quartz.py` | Catches similar comments with different wording |

**Reproducibility:**
- Temperature=0 for deterministic output
- Content-based seed for same diff → same output
- Deterministic file ordering (priority → directory → filename)

## Repo-Specific Rules (Chalcedony)

Chalcedony enforces repo-specific conventions defined in `.quartzcouncil.yml`. If no config exists, Chalcedony is skipped.

### Example Configuration

Create `.quartzcouncil.yml` in your repo root:

```yaml
version: 1

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

  css_modules_access:
    enabled: true
    style_object: "styles"
    bracket_notation_only: true
    severity: warning

  data_attributes:
    enabled: true
    allowed_prefixes: ["data-state", "data-variant", "data-open"]
    severity: warning

policy:
  - id: "hooks-naming"
    severity: warning
    text: "Custom hooks must be named useX and must not be exported as default."
```

### Supported Rules

| Rule | Purpose |
|------|---------|
| `bem_naming` | BEM class naming conventions with configurable prefix/separators |
| `scss_nesting` | Require `&` for nested SCSS selectors |
| `css_modules_access` | Enforce `styles["x"]` vs `styles.x` access patterns |
| `data_attributes` | Restrict data-* attributes to allowed prefixes |
| `extract_utils` | Flag duplicate code that should be extracted |

### Freeform Policies

For custom rules not covered by toggles:

```yaml
policy:
  - id: "unique-id"
    severity: warning
    text: "Description of the rule to enforce"
```

Chalcedony ONLY enforces rules defined in the config — it never invents or suggests improvements beyond what's specified.

## Scripts

### List Installations

See which accounts have installed your GitHub App:

```bash
uv run python scripts/list_installations.py
```

Output:
```
Found 1 installation(s):

------------------------------------------------------------
  User: aperdomoll90
  Installation ID: 105484991
  Created: 2026-01-22T04:17:41.000Z
  Repos: Selected repositories only
------------------------------------------------------------
```

## Roadmap

### Future Triggers

- **GitHub Check Run button** — Trigger reviews via GitHub's native Checks interface instead of a comment

### Planned Council Members

| Agent | Domain | Category |
|-------|--------|----------|
| **Rutile** | Critical-path & interaction performance (hot paths, animation/jank) | `perf` |
| **Smoky** | Accessibility (keyboard, focus, ARIA, reduced-motion) | `a11y` |
| **Onyx** | Node/Next server & security (validation, auth leaks, env exposure) | `security` |
| **Agate** | Architecture (boundaries, ownership, coupling) | `arch` |
| **Phantom** | Refactors & legacy risk (state evolution, regression traps) | `arch` |
| **Rose** | UX heuristics (interaction clarity, comfort, motion restraint) | `ux` |
| **Jasper** | Modern JS idioms (array methods over loops, destructuring, optional chaining) | `style` |
