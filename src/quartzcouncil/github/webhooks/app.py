import os
import hmac
import hashlib
from fastapi import FastAPI, Request, HTTPException

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
            # Phase 1: log only
            repo = payload["repository"]["full_name"]
            pr_url = payload["issue"]["pull_request"]["html_url"]
            print(f"[QuartzCouncil] Triggered review via command. repo={repo} pr={pr_url}")
        
            
            print("[QuartzCouncil] ✅ COMMAND TRIGGERED: /quartz review")
            print("[QuartzCouncil] event=", event, "action=", payload.get("action"))
            print("[QuartzCouncil] repo=", payload["repository"]["full_name"])
            print("[QuartzCouncil] installation_id=", payload.get("installation", {}).get("id"))
            print("[QuartzCouncil] issue_number=", payload["issue"]["number"])
            
            return {"ok": True, "triggered": True}

    # Default: ignore
    return {"ok": True, "triggered": False}
