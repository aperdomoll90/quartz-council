import os
import hmac
import hashlib
import traceback
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

from quartzcouncil.github.auth import get_installation_token
from quartzcouncil.github.pr import fetch_pr_files
from quartzcouncil.github.client.github_client import GitHubClient
from quartzcouncil.github.client.pr_api import fetch_pr_head_sha, find_existing_quartz_review, post_issue_comment
from quartzcouncil.github.client.review_publisher import create_pr_review
from quartzcouncil.core.pr_models import PullRequestInput, PullRequestFile
from quartzcouncil.core.rate_limit import check_rate_limit, record_review, get_retry_after
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

                # Check rate limit before processing
                allowed, remaining = check_rate_limit(installation_id)
                if not allowed:
                    retry_after = get_retry_after(installation_id)
                    print(f"[QuartzCouncil] ⚠️ Rate limited: {owner}/{repo_name} #{pr_number} (retry in {retry_after}s)")
                    return {
                        "ok": False,
                        "triggered": True,
                        "rate_limited": True,
                        "retry_after_seconds": retry_after,
                    }

                print(f"[QuartzCouncil] 🔎 Fetching PR files: {owner}/{repo_name} #{pr_number} (remaining: {remaining})")

                token = await get_installation_token(installation_id)
                gh = GitHubClient(token)

                # Fetch head SHA first for idempotency check
                head_sha = await fetch_pr_head_sha(owner, repo_name, pr_number, gh)

                # Check if we already reviewed this commit (skip if disabled for testing)
                idempotency_enabled = os.getenv("QUARTZ_IDEMPOTENCY_CHECK", "true").lower() == "true"
                if idempotency_enabled:
                    existing_review = await find_existing_quartz_review(owner, repo_name, pr_number, head_sha, gh)
                    if existing_review:
                        print(f"[QuartzCouncil] ⏭️ Already reviewed commit {head_sha[:7]}")
                        feedback_message = (
                            f"A QuartzCouncil review already exists for this commit (`{head_sha[:7]}`).\n\n"
                            f"[View existing review]({existing_review.html_url})\n\n"
                            f"_Push new commits to trigger a fresh review._"
                        )
                        await post_issue_comment(owner, repo_name, pr_number, feedback_message, gh)
                        return {
                            "ok": True,
                            "triggered": True,
                            "skipped": True,
                            "reason": "already_reviewed",
                            "existing_review_url": existing_review.html_url,
                        }

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

                published = await create_pr_review(
                    owner=owner,
                    repo=repo_name,
                    pr_number=pr_number,
                    commit_id=head_sha,
                    summary_md=review.summary,
                    comments=review.comments,
                    files=files,
                    gh=gh,
                )

                review_url = published.get("html_url") or published.get("url")
                print(f"[QuartzCouncil] ✅ Published PR review: {review_url}")

                # Record successful review for rate limiting
                record_review(installation_id)

                return {"ok": True, "triggered": True, "published": True, "comments": len(review.comments)}

            except Exception as error:
                print(f"[QuartzCouncil] ❌ ERROR: {error}")
                traceback.print_exc()

                # Post error feedback to PR
                try:
                    error_message = (
                        f"QuartzCouncil review failed.\n\n"
                        f"```\n{str(error)}\n```\n\n"
                        f"_Please try again or report this issue._"
                    )
                    await post_issue_comment(owner, repo_name, pr_number, error_message, gh)
                except Exception:
                    pass  # Don't fail if we can't post the error comment

                return {"ok": False, "triggered": True, "error": str(error)}

    # Default: ignore
    return {"ok": True, "triggered": False}
