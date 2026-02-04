# QuartzCouncil Architecture Map

**Complete data flow from GitHub webhook to published review**

---

## 🤔 Why This Architecture?

### **Why Two Lambdas + SQS Queue?**

GitHub webhooks have a **10-second timeout**. If your endpoint doesn't respond within 10 seconds, GitHub marks the delivery as failed and may retry (causing duplicate reviews).

Our review pipeline takes **30-90 seconds** (LLM API calls are slow). We can't respond in time.

**Solution:** Split into two Lambdas with SQS in between:

| Component | Responsibility | Time Budget |
|-----------|----------------|-------------|
| **Receiver Lambda** | Verify signature, validate command, enqueue job | <1 second |
| **SQS Queue** | Buffer jobs, handle retries, ensure delivery | N/A |
| **Worker Lambda** | Fetch files, run agents, post review | Up to 3 minutes |

**Why not just one Lambda with async?**
- Lambda can't "fire and forget" — it terminates when the handler returns
- Background threads/tasks get killed when Lambda freezes between invocations
- SQS gives us: retry logic, dead-letter queue, visibility timeout, decoupled scaling

### **Why Receiver Returns 200 Immediately?**

GitHub interprets non-2xx responses as failures and will retry. By returning 200 before processing:
- GitHub sees success instantly (no retries)
- Actual work happens asynchronously
- If processing fails, we handle it ourselves (post error comment to PR)

### **Why a Lambda Layer?**

Python dependencies (LangChain, httpx, pydantic) total ~50MB. Lambda has a 250MB unzipped limit.

**Without layer:** Every deploy uploads all dependencies. Slow, wasteful, hits size limits.

**With layer:** Dependencies deploy once, functions just include our code (~100KB). Fast deploys, stays under limits, shared across functions.

### **Why DynamoDB for Idempotency?**

Lambda can be invoked multiple times for the same event (at-least-once delivery). Without idempotency:
- Same PR gets reviewed twice
- Duplicate comments posted
- Wasted API costs

DynamoDB stores `{commit_sha: review_id}`. Before processing, we check if this commit was already reviewed. If yes, skip and link to existing review.

### **Why Stateless Design?**

No database for rate limiting or user sessions because:
- **Simpler ops** — No RDS/DynamoDB to manage for basic state
- **Cheaper** — Pay only when webhooks fire
- **Scalable** — Each Lambda invocation is independent
- **Resilient** — No single point of failure

Trade-off: Rate limits reset on Lambda cold start. Acceptable for a code review tool.

---

## 🏗️ Infrastructure Overview

### **Production (AWS Lambda)**
```
┌─────────────┐
│   GitHub    │
│   Webhook   │
└──────┬──────┘
       │ POST /github/webhook
       │ Headers: X-Hub-Signature-256, X-GitHub-Event, X-GitHub-Delivery
       │ Body: JSON payload
       ▼
┌──────────────────────────────────────────────────────────┐
│  API Gateway                                             │
│  - Rate limit: 10 req/sec, burst: 20                     │
│  - Stage: Prod                                           │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Receiver Lambda (ReceiverFunction)                      │
│  - Runtime: Python 3.9                                   │
│  - Timeout: 15 seconds                                   │
│  - Memory: 256 MB                                        │
│  - Role: Can send to SQS, read Secrets Manager           │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│  SQS Queue (ReviewQueue)                                 │
│  - Visibility timeout: 300 seconds                       │
│  - Purpose: Decouples webhook from processing            │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│  Worker Lambda (WorkerFunction)                          │
│  - Runtime: Python 3.9                                   │
│  - Timeout: 180 seconds (3 minutes)                      │
│  - Memory: 1024 MB (1 GB)                                │
│  - Layers: QuartzCouncilLayer (all dependencies)         │
│  - Role: Read Secrets Manager, DynamoDB, GitHub API      │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
                 GitHub API
              (Posts PR review)
```

### **Local Development**
```
┌─────────────┐
│   GitHub    │
│   Webhook   │
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  ngrok (Tunnel)                                          │
│  - Public URL: https://xyz.ngrok-free.app                │
│  - Forwards to: localhost:8000                           │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│  FastAPI Server (quartzcouncil/__main__.py)              │
│  - Host: 0.0.0.0:8000                                    │
│  - Reload: True (dev mode)                               │
│  - Endpoint: /github/webhook                             │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
                 GitHub API
              (Posts PR review)
```

---

## 📊 Data Flow Detailed

### **Stage 1: Webhook Reception**

**Location:** `src/quartzcouncil/github/webhooks/app.py` (local) or `infra/sam/functions/receiver/app.py` (AWS)

**Input Format:**
```json
{
  "action": "created",
  "issue": {
    "number": 123,
    "title": "Fix login bug",
    "pull_request": {
      "html_url": "https://github.com/org/repo/pull/123"
    }
  },
  "comment": {
    "body": "/quartz review",
    "user": {
      "login": "homefolder",
      "id": 12345
    }
  },
  "repository": {
    "full_name": "org/repo"
  },
  "installation": {
    "id": 67890
  }
}
```

**Headers:**
```
X-GitHub-Event: issue_comment
X-GitHub-Delivery: abc-123-def-456 (unique delivery ID)
X-Hub-Signature-256: sha256=abc123... (HMAC signature)
```

**Processing Steps:**

1. **Signature Verification** (`_verify_github_signature`)
   ```python
   secret = os.getenv("GITHUB_WEBHOOK_SECRET")
   expected = hmac.new(secret.encode(), raw_body, sha256).hexdigest()
   if expected != received_signature:
       raise HTTPException(401, "Invalid signature")
   ```

2. **Event Filter & Command Parsing** (`_is_pr_issue_comment` + `_parse_quartz_command`)
   ```python
   # Must be:
   # - issue_comment.created event
   # - On a PR (not regular issue)
   # - Comment starts with "/quartz"
   if not all_conditions:
       return {"ok": True, "triggered": False}  # Ignore

   # Supported commands:
   # /quartz review          → Run all agents (default)
   # /quartz                  → Run all agents (default)
   # /quartz amethyst        → Run only Amethyst (TypeScript)
   # /quartz citrine         → Run only Citrine (React/Next)
   # /quartz chalcedony      → Run only Chalcedony (conventions)
   # /quartz amethyst citrine → Run multiple specific agents
   ```

3. **Extract Key Data**
   ```python
   repo_full = "org/repo"
   owner, repo_name = repo_full.split("/")
   installation_id = 67890
   pr_number = 123
   title = "Fix login bug"
   ```

**AWS Only:** Enqueue to SQS and return immediately
```python
sqs.send_message(
    QueueUrl=REVIEW_QUEUE_URL,
    MessageBody=json.dumps({
        "owner": owner,
        "repo": repo_name,
        "pr_number": pr_number,
        "installation_id": installation_id,
        "delivery_id": delivery_id,
        "triggered_by": triggered_by,        # GitHub username
        "triggered_by_id": triggered_by_id,  # GitHub user ID
        "agents": command["agents"],         # None = all, or ["amethyst", "citrine"]
    })
)
return {"statusCode": 200}  # GitHub gets instant response
```

**Local:** Continue to Stage 2 in same process

---

### **Stage 2: Pre-Flight Checks**

**Location:** `src/quartzcouncil/github/webhooks/app.py` (local) or `infra/sam/functions/worker/app.py` (AWS)

**Steps:**

1. **Load Secrets**
   ```python
   # AWS: From Secrets Manager
   os.environ["OPENAI_API_KEY"] = get_secret(OPENAI_API_KEY_ARN)
   os.environ["GITHUB_APP_ID"] = get_secret(GITHUB_APP_ID_ARN)
   os.environ["GITHUB_PRIVATE_KEY_PEM"] = get_secret(GITHUB_PRIVATE_KEY_ARN)
   
   # Local: From .env file
   load_dotenv()  # Reads .env automatically
   ```

2. **Get GitHub Installation Token** (`github/auth.py`)
   ```python
   # Create JWT signed with private key
   jwt_token = create_app_jwt()  # Valid 10 minutes
   
   # Exchange for installation token
   POST https://api.github.com/app/installations/{installation_id}/access_tokens
   Authorization: Bearer {jwt_token}
   
   # Response:
   {
       "token": "ghs_abc123...",  # Valid 1 hour
       "expires_at": "2026-02-01T12:00:00Z"
   }
   ```

3. **Fetch PR Head SHA** (`github/client/pr_api.py`)
   ```python
   GET https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}
   Authorization: token {installation_token}
   
   # Response:
   {
       "head": {
           "sha": "abc123def456..."  # Current commit
       }
   }
   ```

4. **Idempotency Check** (`github/client/pr_api.py::find_existing_quartz_review`)
   ```python
   GET https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews
   
   # Check if any review contains:
   for review in reviews:
       if "## QuartzCouncil Review" in review["body"]:
           if review["commit_id"] == head_sha:
               # Already reviewed this commit!
               return existing_review
   
   # If QUARTZ_IDEMPOTENCY_CHECK=true (default):
   if existing_review:
       return skip_message  # Don't review again
   ```

5. **Rate Limit Check** (`core/rate_limit.py`)
   ```python
   # In-memory store (resets on Lambda restart)
   _rate_limit_store = {
       "org/repo#123": {
           "count": 2,
           "window_start": 1738368000
       }
   }
   
   # Config from .quartzcouncil.yaml in repo (if exists)
   config = {
       "rate_limit": {
           "max_reviews_per_pr_per_hour": 3
       }
   }
   
   if count >= max_reviews:
       return skip_message  # Rate limited
   ```

---

### **Stage 3: Fetch PR Files**

**Location:** `github/pr.py::fetch_pr_files`

**API Call:**
```python
GET https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files
Authorization: token {installation_token}
```

**Response Format:**
```json
[
  {
    "filename": "src/App.tsx",
    "status": "modified",
    "additions": 10,
    "deletions": 3,
    "changes": 13,
    "patch": "@@ -5,7 +5,10 @@ import { User } from './types';\n+const user = data as User;\n-const user: User = data;"
  },
  {
    "filename": "src/utils.ts",
    "status": "added",
    "patch": "@@ -0,0 +1,20 @@\n+export function helper() {\n+  return true;\n+}"
  }
]
```

**Transform to Internal Format:**
```python
from quartzcouncil.core.pr_models import PullRequestFile, PullRequestInput

files = []
for gh_file in github_files:
    if gh_file.get("patch"):  # Only files with diffs
        files.append(PullRequestFile(
            filename=gh_file["filename"],
            patch=gh_file["patch"]
        ))

pr_input = PullRequestInput(
    number=123,
    title="Fix login bug",
    files=files,
    base_sha="old123...",
    head_sha="abc123..."
)
```

**PullRequestInput Schema:**
```python
class PullRequestInput(BaseModel):
    number: int               # 123
    title: str                # "Fix login bug"
    files: list[PullRequestFile]  # See below
    base_sha: Optional[str]   # "old123..." (before changes)
    head_sha: Optional[str]   # "abc123..." (after changes)
```

**PullRequestFile Schema:**
```python
class PullRequestFile(BaseModel):
    filename: str  # "src/App.tsx"
    patch: str     # Unified diff format (see below)
```

**Patch Format (Unified Diff):**
```diff
@@ -5,7 +5,10 @@ import { User } from './types';
 
 export function Login() {
   const [data, setData] = useState<any>();
+  
+  // Get user data
+  const user = data as User;  // Unsafe cast!
+  console.log(user.name);
-  const user: User = data;
-  if (user) console.log(user.name);
 
   return <div>Login</div>;
 }
```

---

### **Stage 4: Route Files to Agents**

**Location:** `agents/quartz.py::review_council`

**File Type Detection:**
```python
# File extension mapping
AMETHYST_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}
CITRINE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}

# Filter files by agent
def _filter_files_for_agent(files, extensions):
    return [f for f in files if Path(f.filename).suffix in extensions]

amethyst_files = _filter_files_for_agent(pr_input.files, AMETHYST_EXTENSIONS)
citrine_files = _filter_files_for_agent(pr_input.files, CITRINE_EXTENSIONS)
```

**File Prioritization** (`agents/base.py::_get_file_priority`):
```python
def _get_file_priority(filepath: str) -> int:
    """Lower number = higher priority"""
    # Priority 0: Components (user-facing)
    if "/components/" in filepath or filepath.endswith(".tsx"):
        return 0
    # Priority 1: Hooks (shared logic)
    if "/hooks/" in filepath and "use" in filename:
        return 1
    # Priority 2: Pages/routes
    if "/pages/" in filepath or "/app/" in filepath:
        return 2
    # Priority 3: Utils
    if "/utils/" in filepath or "/helpers/" in filepath:
        return 3
    # Priority 4: Tests
    if ".test." in filepath or "__tests__" in filepath:
        return 4
    # Priority 5: Config/generated
    if "config" in filepath or ".gen." in filepath:
        return 5
    return 3  # Default: utils-level
```

**Batching** (`agents/base.py::chunk_files_by_char_budget`):
```python
MAX_CHARS_PER_BATCH = 40_000   # ~10-15 files
MAX_FILES_PER_BATCH = 12
MAX_PATCH_SIZE = 60_000        # Skip huge minified files

# Sort by priority, then directory, then filename
sorted_files = sorted(files, key=lambda f: (
    _get_file_priority(f.filename),
    f.filename.rsplit('/', 1)[0],
    f.filename
))

batches = []
current_batch = []
current_chars = 0

for file in sorted_files:
    patch_size = len(file.patch)
    
    # Skip gigantic files
    if patch_size > MAX_PATCH_SIZE:
        skipped_files.append(file.filename)
        continue
    
    # Start new batch if limits exceeded
    if current_chars + patch_size > MAX_CHARS_PER_BATCH or len(current_batch) >= MAX_FILES_PER_BATCH:
        batches.append(current_batch)
        current_batch = []
        current_chars = 0
    
    current_batch.append(file)
    current_chars += patch_size

if current_batch:
    batches.append(current_batch)
```

**Cap Batches to Control Costs:**
```python
MAX_BATCHES_PER_AGENT = 5  # Max 5 batches per agent

if len(batches) > MAX_BATCHES_PER_AGENT:
    print(f"⚠️ Capping at {MAX_BATCHES_PER_AGENT} batches")
    skipped_count = sum(len(b) for b in batches[MAX_BATCHES_PER_AGENT:])
    warnings.append(ReviewWarning(
        kind="rate_limited",
        message=f"PR too large: skipped {skipped_count} files"
    ))
    batches = batches[:MAX_BATCHES_PER_AGENT]
```

---

### **Stage 5: Run Agents in Parallel**

**Location:** `agents/quartz.py::review_council`

**Create Agent-Specific PRs:**
```python
amethyst_pr = PullRequestInput(
    number=123,
    title="Fix login bug",
    files=amethyst_files,  # Only .ts/.tsx files
    base_sha=pr_input.base_sha,
    head_sha=pr_input.head_sha
)

citrine_pr = PullRequestInput(
    number=123,
    title="Fix login bug",
    files=citrine_files,   # Only .ts/.tsx files (same for now)
    base_sha=pr_input.base_sha,
    head_sha=pr_input.head_sha
)
```

**Parallel Execution:**
```python
import asyncio

# Core agents (Amethyst + Citrine) run in parallel
core_tasks = []
if amethyst_files and amethyst_enabled:
    core_tasks.append(review_amethyst(amethyst_pr))
if citrine_files and citrine_enabled:
    core_tasks.append(review_citrine(citrine_pr))

# Chalcedony runs separately if config exists and has rules
chalcedony_task = None
if cfg is not None and cfg.has_any_rules() and chalcedony_enabled:
    chalcedony_task = review_chalcedony(pr, cfg)

# All agents run simultaneously
all_tasks = core_tasks + ([chalcedony_task] if chalcedony_task else [])
results = await asyncio.gather(*all_tasks)

# AgentResult format:
# {
#     "comments": [ReviewComment, ...],
#     "warnings": [ReviewWarning, ...],
#     "token_usage": [TokenUsage, ...]
# }
```

**Agent Toggle Priority:**
```python
# Priority: agents_override (from /quartz command) > config > defaults
if agents_override is not None:
    # /quartz amethyst citrine → only run those
    amethyst_enabled = "amethyst" in agents_override
    citrine_enabled = "citrine" in agents_override
    chalcedony_enabled = "chalcedony" in agents_override
elif cfg is not None:
    # Use .quartzcouncil.yml agent toggles
    amethyst_enabled = cfg.agents.amethyst
    citrine_enabled = cfg.agents.citrine
    chalcedony_enabled = cfg.agents.chalcedony
else:
    # All enabled by default
    amethyst_enabled = citrine_enabled = chalcedony_enabled = True
```

---

### **Stage 6: Agent Execution (Batched)**

**Location:** `agents/base.py::run_review_agent_batched`

**For Each Batch:**

1. **Build Diff with Line Numbers** (`agents/base.py::build_diff`)
   ```python
   def _add_line_numbers_to_patch(patch: str) -> str:
       # Transform:
       "@@ -5,7 +5,10 @@"
       "+const user = data as User;"
       
       # To:
       "@@ -5,7 +5,10 @@"
       "L   5 +const user = data as User;"
       "L   6 +console.log(user.name);"
   ```

   **Output Format:**
   ```diff
   --- FILE: src/App.tsx ---
   @@ -5,7 +5,10 @@ import { User } from './types';
   L   5  
   L   6  export function Login() {
   L   7    const [data, setData] = useState<any>();
   L   8 +  
   L   9 +  // Get user data
   L  10 +  const user = data as User;  // Unsafe cast!
   L  11 +  console.log(user.name);
          -  const user: User = data;
          -  if (user) console.log(user.name);
   L  12  
   L  13    return <div>Login</div>;
   L  14  }
   ```

2. **Compute Content-Based Seed** (`agents/base.py::_compute_content_seed`)
   ```python
   # SHA256 hash of diff content
   content_hash = hashlib.sha256(diff_content.encode()).hexdigest()
   # Take first 8 hex chars → int
   seed = int(content_hash[:8], 16)  # e.g., 2847562910
   
   # Same diff always produces same seed → deterministic LLM output
   ```

3. **Create LLM with Seed** (`agents/base.py`)
   ```python
   from langchain_openai import ChatOpenAI

   model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
   temperature = float(os.getenv("OPENAI_TEMPERATURE", "0"))

   llm = ChatOpenAI(
       model=model,
       temperature=temperature,
       model_kwargs={"seed": content_seed}  # For reproducibility
   )
   ```

4. **Build Prompt** (Agent-Specific)

**Amethyst Prompt** (`agents/amethyst.py`):
```python
SYSTEM_PROMPT = """You are Amethyst, a TypeScript correctness and type safety reviewer.

MISSION
Find only high-signal type-safety issues that could cause:
- runtime bugs
- incorrect behavior
- unsafe public APIs

FOCUS (report these)
- any/unknown misuse that bypasses checks
- unsafe casts/as assertions that can be wrong at runtime
- missing type narrowing / guards that can throw
- incorrect generics, inference traps
- Zod schema drift vs inferred types

DO NOT REPORT
- style, formatting, naming
- "nice to have" annotations
- refactors that are subjective
- hypothetical issues without evidence

RULES
- Only comment on code present in the diff
- Prefer zero comments over noisy comments
- Severity mapping:
  - error: likely runtime bug or unsafe API
  - warning: probable issue / footgun
  - info: NEVER USE (deleted by filters)
- If unsure, OMIT the comment

OUTPUT
Return at most 10 ReviewComment objects."""

USER_PROMPT = """Review the following PR diff:

{diff}

Return structured ReviewComment objects. If no issues, return []."""
```

**Citrine Prompt** (`agents/citrine.py`):
```python
SYSTEM_PROMPT = """You are Citrine, a React/Next.js performance and architecture reviewer.

FOCUS (report these)
- Unnecessary re-renders and memo misuse
- useEffect lifecycle issues (missing deps, cleanup)
- Event listener leaks
- Server/client component boundary violations
- Hook correctness (rules of hooks)

DO NOT REPORT
- Purely aesthetic CSS
- Business logic (unless UI/perf impact)
- Type safety (that's Amethyst's job)

RULES
- Only comment on code in the diff
- error: causes bugs or major perf issues
- warning: suboptimal patterns
- If unsure, OMIT

OUTPUT
Max 10 ReviewComment objects."""
```

**Common Instructions** (`prompts/shared.py`):
```python
LINE_NUMBER_ACCURACY = """LINE NUMBER ACCURACY (CRITICAL)
The diff includes line numbers in format "L  42 +code here".
When reporting issues:
- Use the EXACT line number from "L  42" format
- line_start and line_end must match visible L numbers
- Never reference lines outside the diff
- If issue spans lines 10-12, use line_start=10, line_end=12"""

SHARED_RULES = """
- Prefer fewer comments; avoid repeating the same point
- Never flag the same issue twice
- Be specific about what's wrong and why it matters
"""

# Combined into agent prompts
```

5. **Call LLM** (`agents/base.py::run_review_agent`)
   ```python
   from langchain_core.prompts import ChatPromptTemplate
   
   prompt = ChatPromptTemplate.from_messages([
       ("system", SYSTEM_PROMPT),
       ("user", USER_PROMPT)
   ])
   
   structured_llm = llm.with_structured_output(AgentOutput)
   chain = prompt | structured_llm
   
   result = await chain.ainvoke({"diff": diff_content})
   ```

6. **LLM Output Format** (Pydantic Schema)
   ```python
   class AgentOutput(BaseModel):
       comments: list[RawComment]
   
   class RawComment(BaseModel):
       file: str                # "src/App.tsx"
       line_start: int          # 10
       line_end: int            # 10
       severity: Literal["error", "warning", "info"]
       category: Literal["types", "perf", "arch", ...]
       message: str             # "Unsafe cast from any to User"
       suggestion: Optional[str]  # "Add runtime validation: if (isUser(data))"
   ```

   **Example LLM Response:**
   ```json
   {
     "comments": [
       {
         "file": "src/App.tsx",
         "line_start": 10,
         "line_end": 10,
         "severity": "error",
         "category": "types",
         "message": "Unsafe type assertion from any to User without runtime validation",
         "suggestion": "Add a type guard: if (data && typeof data === 'object' && 'name' in data)"
       },
       {
         "file": "src/App.tsx",
         "line_start": 11,
         "line_end": 11,
         "severity": "error",
         "category": "types",
         "message": "Accessing property 'name' on User without null check. data is typed as any and could be undefined.",
         "suggestion": "Add null check: if (user) { console.log(user.name); }"
       }
     ]
   }
   ```

7. **Inject Agent Name** (Code, not LLM)
   ```python
   # LLM returns RawComment (no agent field)
   # Code injects agent deterministically
   comments = [
       ReviewComment(
           agent="Amethyst",  # Injected by code
           **raw.model_dump()  # All fields from RawComment
       )
       for raw in result.comments
   ]
   ```

   **ReviewComment Schema:**
   ```python
   class ReviewComment(RawComment):
       agent: Literal["Amethyst", "Citrine", "Chalcedony"]
   ```

8. **Filter Low-Quality Comments** (`agents/base.py::_filter_low_quality_comments`)
   
   **Hedging Phrases (Dropped):**
   ```python
   HEDGING_PHRASES = [
       "consider ",
       "might want to",
       "you should consider",
       "would recommend",
       "for better safety"
   ]
   
   # Example dropped comment:
   # "Consider adding a null check for better safety"
   # → DROPPED (advice-style, not a bug)
   ```
   
   **False Positive Patterns (Dropped):**
   ```python
   FALSE_POSITIVE_PATTERNS = [
       ("infinite loop", None, "error"),
       ("infinite re-render", None, "error"),
       ("setstate", "useeffect", "error"),  # setState in useEffect is not always wrong
       ("can throw", None, "error"),  # Speculative
       ("can cause", None, "error"),   # Speculative
   ]
   
   # Example dropped comment:
   # severity="error", message="This can cause an infinite loop"
   # → DROPPED (speculative, no proof)
   ```
   
   **Info-Level Comments (Dropped):**
   ```python
   if comment.severity == "info":
       # Info should never be used per prompts
       # If LLM outputs info, it's hedging
       return True  # Drop it
   ```

9. **Merge Batch Results**
   ```python
   all_comments = []
   for batch_index, batch_files in enumerate(batches):
       batch_pr = PullRequestInput(...)
       comments, batch_failed = await run_review_agent(
           batch_pr, "Amethyst", prompt
       )
       all_comments.extend(comments)
   
   return AgentResult(
       comments=all_comments,
       warnings=[...]
   )
   ```

---

### **Stage 7: Merge Agent Results**

**Location:** `agents/quartz.py::review_council`

**Combine Results:**
```python
amethyst_result: AgentResult  # {comments: [...], warnings: [...]}
citrine_result: AgentResult   # {comments: [...], warnings: [...]}

all_comments = amethyst_result.comments + citrine_result.comments
all_warnings = amethyst_result.warnings + citrine_result.warnings

# Example:
# all_comments = [
#   ReviewComment(agent="Amethyst", severity="error", file="App.tsx", line_start=10, ...),
#   ReviewComment(agent="Amethyst", severity="warning", file="utils.ts", line_start=5, ...),
#   ReviewComment(agent="Citrine", severity="error", file="App.tsx", line_start=20, ...),
#   ReviewComment(agent="Citrine", severity="warning", file="Component.tsx", line_start=15, ...),
# ]
```

---

### **Stage 8: Sanitize & Deduplicate**

**Location:** `agents/quartz.py::_deduplicate_and_sanitize`

**Step 1: Sanitize Messages** (Remove noise words)
```python
NOISE_WORDS = {
    "a", "an", "the", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "should",
    "could", "may", "might", "must", "can",
    "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they",
    "what", "which", "who", "when", "where", "why", "how",
    "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "only", "same",
    "so", "than", "too", "very"
}

def _sanitize_message(message: str) -> str:
    words = message.lower().split()
    # Remove noise words
    meaningful = [w for w in words if w not in NOISE_WORDS]
    return " ".join(meaningful)

# Example:
# Input:  "This is an unsafe cast that could cause issues"
# Output: "unsafe cast cause issues"
```

**Step 2: Compute Message Fingerprint** (Content-based dedup)
```python
def _compute_message_fingerprint(comment: ReviewComment) -> str:
    sanitized = _sanitize_message(comment.message)
    # Bucket line to nearest 5 (e.g., 10-14 → bucket 10)
    line_bucket = (comment.line_start // 5) * 5
    # Combine file + line bucket + category + message
    content = f"{comment.file}:{line_bucket}:{comment.category}:{sanitized[:100]}"
    return hashlib.md5(content.encode()).hexdigest()

# Example:
# file="App.tsx", line_start=10, category="types", message="unsafe cast..."
# fingerprint="a3f7b2c1..." (MD5 hash)
```

**Step 3: Check Overlapping Lines**
```python
def _comments_overlap(c1: ReviewComment, c2: ReviewComment) -> bool:
    # Different files can't overlap
    if c1.file != c2.file:
        return False
    
    # Check if line ranges overlap
    # c1: lines 10-12
    # c2: lines 11-13
    # → Overlaps? YES (line 11 and 12 are shared)
    
    c1_after_c2 = c1.line_start > c2.line_end     # 10 > 13? NO
    c1_before_c2 = c1.line_end < c2.line_start    # 12 < 11? NO
    
    return not (c1_after_c2 or c1_before_c2)  # YES, they overlap
```

**Step 4: Deduplication Strategy**
```python
def _deduplicate(
    comments: list[ReviewComment],
    max_comments: int = 20,
    merge_overlapping: bool = True,  # Combine same-line issues
    content_similarity: bool = True,  # Use fingerprints
) -> list[ReviewComment]:
    
    # Sort by severity (error > warning), then file, then line
    severity_rank = {"error": 3, "warning": 2, "info": 1}
    sorted_comments = sorted(comments, key=lambda c: (
        -severity_rank[c.severity],  # Errors first
        c.file,
        c.line_start
    ))
    
    kept = []
    seen_fingerprints = set()
    
    for comment in sorted_comments:
        # Check 1: Content-based duplicate?
        fingerprint = _compute_message_fingerprint(comment)
        if fingerprint in seen_fingerprints:
            print(f"DROPPED (duplicate message): {comment.message[:60]}...")
            continue
        
        # Check 2: Location-based duplicate?
        overlapping = [c for c in kept if _comments_overlap(comment, c)]
        
        if overlapping and merge_overlapping:
            # Merge into combined comment
            existing = overlapping[0]
            merged = _merge_comments(existing, comment)
            kept[kept.index(existing)] = merged
            print(f"MERGED: {comment.agent} + {existing.agent} at {comment.file}:{comment.line_start}")
            continue
        elif overlapping:
            # Keep first, drop rest
            print(f"DROPPED (same location): {comment.message[:60]}...")
            continue
        
        # Keep this comment
        kept.append(comment)
        seen_fingerprints.add(fingerprint)
        
        # Stop at max comments
        if len(kept) >= max_comments:
            break
    
    return kept
```

**Merged Comment Format:**
```python
def _merge_comments(c1: ReviewComment, c2: ReviewComment) -> ReviewComment:
    # Combine messages with bullet points
    combined_message = (
        f"Multiple issues found:\n\n"
        f"• [{c1.agent}] {c1.message}\n"
        f"• [{c2.agent}] {c2.message}"
    )
    
    # Take highest severity
    severity = "error" if "error" in [c1.severity, c2.severity] else "warning"
    
    return ReviewComment(
        agent="Combined",  # Special marker
        file=c1.file,
        line_start=min(c1.line_start, c2.line_start),
        line_end=max(c1.line_end, c2.line_end),
        severity=severity,
        category=c1.category,  # First comment's category
        message=combined_message,
        suggestion=None  # Drop suggestions in merged comments
    )
```

---

### **Stage 9: Generate Summary**

**Location:** `agents/quartz.py::_generate_summary`

**Input:** Final deduplicated comments
```python
comments = [
    ReviewComment(agent="Amethyst", severity="error", category="types", ...),
    ReviewComment(agent="Citrine", severity="warning", category="perf", ...),
    ReviewComment(agent="Combined", severity="error", category="types", ...),
]
```

**Processing:**
```python
def _generate_summary(comments: list[ReviewComment]) -> str:
    if not comments:
        return "No issues found. The code looks good."
    
    # Count by severity
    error_count = sum(1 for c in comments if c.severity == "error")
    warning_count = sum(1 for c in comments if c.severity == "warning")
    
    # Count by category
    by_category = {}
    for c in comments:
        by_category[c.category] = by_category.get(c.category, 0) + 1
    
    # Determine risk level
    if error_count > 0:
        risk = "HIGH"
    elif warning_count > 2:
        risk = "MEDIUM"
    else:
        risk = "LOW"
    
    # Build summary
    lines = [
        f"**Risk Level:** {risk}",
        "",
        f"**Issues Found:** {len(comments)} total",
        f"- Errors: {error_count}",
        f"- Warnings: {warning_count}",
        "",
        "**By Category:**"
    ]
    
    for category, count in sorted(by_category.items()):
        lines.append(f"- {category}: {count}")
    
    # Add top concerns (first 3 errors)
    errors = [c for c in comments if c.severity == "error"]
    if errors:
        lines.append("")
        lines.append("**Top Concerns:**")
        for error in errors[:3]:
            preview = error.message[:80] + "..." if len(error.message) > 80 else error.message
            lines.append(f"- [{error.file}:{error.line_start}] {preview}")
    
    return "\n".join(lines)
```

**Example Summary:**
```markdown
**Risk Level:** HIGH

**Issues Found:** 5 total
- Errors: 2
- Warnings: 3

**By Category:**
- perf: 2
- types: 3

**Top Concerns:**
- [src/App.tsx:10] Unsafe type assertion from any to User without runtime valid...
- [src/Component.tsx:42] Missing useEffect cleanup for event listener

---
_Triggered by @homefolder_
_Tokens: 12,450 (~$0.003)_
```

**Token Tracking in Summary:**
```python
# ReviewMeta tracks who triggered and token usage
meta = ReviewMeta(
    triggered_by=triggered_by,           # "@homefolder"
    triggered_by_id=triggered_by_id,     # 12345
    token_usage=all_token_usage,         # Aggregated from all agents
)

# Summary includes attribution and cost
if meta.triggered_by:
    summary += f"\n_Triggered by @{meta.triggered_by}_"
if meta.total_tokens > 0:
    cost = meta.total_cost_usd(model)
    summary += f"\n_Tokens: {meta.total_tokens:,} (~${cost:.3f})_"
```

---

### **Stage 10: Publish to GitHub**

**Location:** `github/client/review_publisher.py::create_pr_review`

**Format Inline Comments:**
```python
def format_inline_comment(comment: ReviewComment) -> str:
    severity_label = comment.severity.upper()
    header = f"**{comment.agent}** · **{severity_label}** · `{comment.category}`"
    body = comment.message.strip()
    if comment.suggestion:
        body += f"\n\n**Suggestion:**\n{comment.suggestion.strip()}"
    return f"{header}\n\n{body}"

# Example output:
# "**Amethyst** · **ERROR** · `types`
# 
# Unsafe type assertion from any to User without runtime validation
# 
# **Suggestion:**
# Add a type guard: if (data && typeof data === 'object' && 'name' in data)"
```

**Format Summary:**
```python
def format_summary(summary_md: str, posted: int, skipped: int) -> str:
    lines = [
        "## QuartzCouncil Review",
        "",
        summary_md.strip(),
        "",
        f"**Inline comments posted:** {posted}",
        f"**Items included only in summary:** {skipped}",
        "",
        "_Triggered via `/quartz review`_",
    ]
    return "\n".join(lines)
```

**Convert to GitHub Format:**
```python
def to_github_review_comment(comment: ReviewComment, commit_id: str) -> dict:
    return {
        "path": comment.file,        # "src/App.tsx"
        "line": comment.line_start,  # 10
        "side": "RIGHT",             # Comment on new code (not old)
        "body": format_inline_comment(comment),
        "commit_id": commit_id       # "abc123def456..."
    }

# GitHub API format
inline_comments = [
    {
        "path": "src/App.tsx",
        "line": 10,
        "side": "RIGHT",
        "body": "**Amethyst** · **ERROR** · `types`\n\nUnsafe type assertion...",
        "commit_id": "abc123..."
    },
    # ... more comments
]
```

**API Call with Fallback:**
```python
async def create_pr_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    summary_md: str,
    comments: list[ReviewComment],
    gh: GitHubClient,
    max_inline: int = 20
) -> dict:
    
    # Limit inline comments
    limited_comments = comments[:max_inline]
    skipped_count = len(comments) - len(limited_comments)
    
    # Build request body
    request_body = {
        "event": "COMMENT",  # Not "APPROVE" or "REQUEST_CHANGES"
        "body": format_summary(summary_md, posted=len(limited_comments), skipped=skipped_count),
        "commit_id": commit_id,
        "comments": [to_github_review_comment(c, commit_id) for c in limited_comments]
    }
    
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    
    try:
        # Try with inline comments
        return await gh.post_json(url, request_body)
    except Exception as e:
        # Most common failure: 422 (line mapping invalid)
        # GitHub can't map line to diff
        print(f"⚠️ Inline comments failed ({e}), posting summary only")
        
        # Fallback: summary-only review
        fallback_body = {
            "event": "COMMENT",
            "body": format_summary(summary_md, posted=0, skipped=len(comments)),
            "commit_id": commit_id
        }
        return await gh.post_json(url, fallback_body)
```

**GitHub API Response:**
```json
{
  "id": 987654321,
  "user": {
    "login": "quartzcouncil[bot]"
  },
  "body": "## QuartzCouncil Review\n\n**Risk Level:** HIGH\n...",
  "state": "COMMENTED",
  "html_url": "https://github.com/org/repo/pull/123#pullrequestreview-987654321",
  "submitted_at": "2026-02-01T10:30:00Z",
  "commit_id": "abc123..."
}
```

---

## 🔄 Complete Data Transformation Chain

```
┌─────────────────────────────────────────────────────────────┐
│  Stage 1: GitHub Webhook (JSON)                             │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Extracted: {owner, repo, pr_number, installation_id}       │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 3: GitHub API Response (File List)                   │
│  [{"filename": "App.tsx", "patch": "@@ ..."}]                │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Transformed: PullRequestInput                               │
│  {number: 123, files: [PullRequestFile, ...]}               │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 4: Filtered & Batched                                 │
│  Amethyst: [[file1, file2], [file3]]  (2 batches)           │
│  Citrine:  [[file1, file2], [file3]]  (2 batches)           │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 5-6: LLM Input (Diff with Line Numbers)              │
│  "--- FILE: App.tsx ---                                     │
│   L  10 +const user = data as User;"                        │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  LLM Output: AgentOutput                                     │
│  {comments: [RawComment, ...]}                               │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Transformed: ReviewComment[] (agent injected)               │
│  [{agent: "Amethyst", file: "App.tsx", ...}]                 │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 7-8: Filtered & Deduplicated                          │
│  - Hedging removed                                           │
│  - False positives removed                                   │
│  - Duplicates merged                                         │
│  - Capped at 20 comments                                     │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 9: CouncilReview                                      │
│  {comments: [ReviewComment, ...], summary: "**Risk...", ... }│
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 10: GitHub PR Review (API Request)                    │
│  {event: "COMMENT", body: "## QuartzCouncil...",             │
│   comments: [{path: "App.tsx", line: 10, ...}]}              │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
                    GitHub PR (Published!)
```

---

## 📦 Common Components Used Throughout

### **1. GitHubClient** (`github/client/github_client.py`)
```python
class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json"
        }
    
    async def get_json(self, url: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)
            resp.raise_for_status()
            return resp.json()
    
    async def post_json(self, url: str, data: dict) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=self.headers, json=data)
            resp.raise_for_status()
            return resp.json()
```

**Used by:**
- `github/auth.py` (get installation token)
- `github/pr.py` (fetch PR files)
- `github/client/pr_api.py` (fetch PR details, check existing reviews)
- `github/client/review_publisher.py` (post review)

### **2. Pydantic Models** (`core/types.py`, `core/pr_models.py`)
```python
# Input format
class PullRequestFile(BaseModel):
    filename: str
    patch: str

class PullRequestInput(BaseModel):
    number: int
    title: str
    files: list[PullRequestFile]

# LLM output
class RawComment(BaseModel):
    file: str
    line_start: int
    line_end: int
    severity: Literal["error", "warning", "info"]
    category: Literal["types", "perf", ...]
    message: str
    suggestion: Optional[str]

# Final output
class ReviewComment(RawComment):
    agent: Literal["Amethyst", "Citrine", ...]

# Warnings
class ReviewWarning(BaseModel):
    kind: Literal["skipped_large_file", "batch_output_limit", "rate_limited"]
    message: str
    file: Optional[str]

# Token usage tracking
class TokenUsage(BaseModel):
    agent: str           # "Amethyst", "Citrine", "Chalcedony"
    batch_index: int     # Which batch (0, 1, 2...)
    input_tokens: int    # Prompt tokens
    output_tokens: int   # Completion tokens

# Review metadata (aggregates token usage + trigger info)
class ReviewMeta(BaseModel):
    triggered_by: Optional[str]      # GitHub username
    triggered_by_id: Optional[int]   # GitHub user ID
    token_usage: list[TokenUsage]    # All token usage across agents

    @property
    def total_tokens(self) -> int:
        return sum(t.input_tokens + t.output_tokens for t in self.token_usage)

    def total_cost_usd(self, model: str) -> float:
        # Uses per-model pricing (gpt-4o-mini: $0.15/$0.60 per 1M tokens)
        ...
```

**Used everywhere** for type safety and validation

### **3. Environment Config** (`.env` + `os.getenv`)
```python
# OpenAI settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0"))

# GitHub credentials
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_PRIVATE_KEY_PATH = os.getenv("GITHUB_PRIVATE_KEY_PATH")
GITHUB_PRIVATE_KEY_PEM = os.getenv("GITHUB_PRIVATE_KEY_PEM")  # AWS

# Feature flags
QUARTZ_IDEMPOTENCY_CHECK = os.getenv("QUARTZ_IDEMPOTENCY_CHECK", "true")
```

### **4. LangChain LLM Setup** (`agents/base.py`)
```python
from langchain_openai import ChatOpenAI

# LLM is created directly in base.py, not via a separate llm.py module
model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
temperature = float(os.getenv("OPENAI_TEMPERATURE", "0"))

llm = ChatOpenAI(
    model=model,
    temperature=temperature,
    model_kwargs={"seed": content_seed} if content_seed else {}
)
```

**Used by:** All agents via `run_review_agent_batched`

---

## 🎯 Key Design Decisions

### **Why Two-Stage Comments (RawComment → ReviewComment)?**
- **LLM doesn't know which agent it is** (prevents hallucination)
- **Code deterministically injects** agent name (can't be wrong)
- **Cleaner validation** (LLM output is simpler schema)

### **Why Batching?**
- **Token limits:** gpt-4o-mini has 16K output limit
- **Large PRs:** 50-file PRs would exceed context window
- **Cost control:** Can cap at 5 batches max

### **Why Content-Based Seed?**
- **Reproducibility:** Same diff → same review
- **Debugging:** Can replay exact LLM call
- **Testing:** Deterministic output for eval

### **Why Line Number Injection in Diff?**
- **LLM accuracy:** "L  42" is clearer than parsing hunks
- **Validation:** Prevents reporting non-existent lines
- **User clarity:** Easier to map issues to code

### **Why Merge Overlapping Comments?**
- **Noise reduction:** "Unsafe cast" + "Missing null check" on same line → 1 combined comment
- **User experience:** Easier to address 1 merged issue than 2 overlapping ones

### **Why Summary-Only Fallback?**
- **GitHub API limitations:** Line mapping can fail (422 error)
- **Graceful degradation:** Review still posted, just without inline comments
- **User value:** Summary alone is better than nothing

---

## 🔒 Security & Secrets Flow

### **Local Development:**
```
.env file
  ↓
python-dotenv (load_dotenv())
  ↓
os.environ["OPENAI_API_KEY"]
  ↓
Used by agents/GitHub client
```

### **AWS Lambda:**
```
AWS Secrets Manager
  ↓
_load_secrets_to_env() in worker Lambda
  ↓
os.environ["OPENAI_API_KEY"]
  ↓
Used by agents/GitHub client
```

**Secrets Never Logged:**
- Webhook signature verified but never printed
- API keys accessed via `os.getenv`, never in logs
- Private key content never logged (only path or "loaded")

---

## 📈 Performance Characteristics

| Stage | Latency | Bottleneck |
|-------|---------|------------|
| Webhook Reception | <50ms | Signature verification |
| Pre-flight Checks | ~500ms | GitHub API calls (token, PR details) |
| Fetch PR Files | ~200ms | GitHub API |
| Agent Execution (per batch) | ~5-10s | OpenAI API call |
| Total (small PR, 2 batches) | ~15-20s | Agent execution |
| Total (large PR, 10 batches) | ~60-90s | Agent execution |

**AWS Lambda Cost (Typical Review):**
- Receiver Lambda: $0.00001 (minimal)
- Worker Lambda: $0.001-0.005 (depends on PR size)
- **Total AWS:** ~$0.005 per review

**OpenAI Cost (Typical Review):**
- Input: ~10K tokens @ $0.15/1M = $0.0015
- Output: ~2K tokens @ $0.60/1M = $0.0012
- **Total OpenAI:** ~$0.003 per review

**Total Cost per Review:** ~$0.008 (< 1 cent)

---

This architecture map shows the complete journey from a `/quartz review` comment to a published GitHub PR review. Every data transformation, filter, and decision point is documented with actual code patterns and formats.
