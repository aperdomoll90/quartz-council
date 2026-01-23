import os
import hmac
import hashlib
import traceback
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

from quartzcouncil.github.auth import get_installation_token
from quartzcouncil.github.pr import fetch_pr_files
from quartzcouncil.core.pr_models import PullRequestInput, PullRequestFile
from quartzcouncil.agents.quartz import review_council

load_dotenv()

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

def _verify_github_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Verify X-Hub-Signature-256 using your webhook secret."""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        # In dev you can allow missing secret, but production should fail hard
        raise HTTPException(status_code=500, detail="Missing GITHUB_WEBHOOK_SECRET")

    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing/invalid signature header")

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=401, detail="Invalid signature")

def _is_pr_issue_comment(payload: dict) -> bool:
    # Issue comments can be on Issues or PRs. PRs include pull_request object.
    issue = payload.get("issue") or {}
    return "pull_request" in issue

def _is_quartz_review_command(payload: dict) -> bool:
    comment = payload.get("comment") or {}
    body = (comment.get("body") or "").strip()
    return body.startswith("/quartz review")

@app.post("/github/webhook")
async def github_webhook(request: Request):
    raw = await request.body()
    event = request.headers.get("X-GitHub-Event", "")
    sig = request.headers.get("X-Hub-Signature-256")

    # Verify authenticity (do this before parsing)
    _verify_github_signature(raw, sig)

    payload = await request.json()

    # GitHub ping
    if event == "ping":
        return {"ok": True, "msg": "pong"}

    # Only trigger on explicit command in PR conversation
    if event == "issue_comment" and payload.get("action") == "created":
        if _is_pr_issue_comment(payload) and _is_quartz_review_command(payload):
            try:
                repo_full = payload["repository"]["full_name"]
                owner, repo_name = repo_full.split("/")

                installation_id = int(payload["installation"]["id"])
                pr_number = int(payload["issue"]["number"])
                title = payload["issue"]["title"]

                print(f"[QuartzCouncil] 🔎 Fetching PR files: {owner}/{repo_name} #{pr_number}")

                token = await get_installation_token(installation_id)
                gh_files = await fetch_pr_files(owner, repo_name, pr_number, token)

                files: list[PullRequestFile] = []
                for gh_file in gh_files:
                    patch = gh_file.get("patch")
                    if not patch:
                        continue
                    files.append(PullRequestFile(filename=gh_file["filename"], patch=patch))

                pr_input = PullRequestInput(number=pr_number, title=title, files=files)

                print(f"[QuartzCouncil] 🤖 Running council on {len(files)} patched files...")
                review = await review_council(pr_input)

                print("[QuartzCouncil] ✅ COUNCIL SUMMARY\n" + review.summary)
                print(f"[QuartzCouncil] ✅ COMMENTS: {len(review.comments)}")
                for comment in review.comments:
                    print(f"[QuartzCouncil] [{comment.agent}] {comment.file}:{comment.line_start}-{comment.line_end} {comment.severity} {comment.category} — {comment.message}")

                return {"ok": True, "triggered": True, "comments": len(review.comments)}

            except Exception as error:
                print(f"[QuartzCouncil] ❌ ERROR: {error}")
                traceback.print_exc()
                return {"ok": False, "triggered": True, "error": str(error)}

    # Default: ignore
    return {"ok": True, "triggered": False}
