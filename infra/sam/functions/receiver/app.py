"""
QuartzCouncil Receiver Lambda

Lightweight webhook handler that:
1. Verifies GitHub webhook signature
2. Checks for /quartz review command
3. Enqueues job to SQS
4. Returns 200 immediately (prevents webhook timeout)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import boto3

sqs = boto3.client("sqs")
secrets = boto3.client("secretsmanager")

# Cache secret to avoid repeated API calls within same Lambda instance
_cached_webhook_secret: str | None = None


def _get_webhook_secret() -> str:
    global _cached_webhook_secret
    if _cached_webhook_secret is None:
        secret_arn = os.environ["GITHUB_WEBHOOK_SECRET_ARN"]
        response = secrets.get_secret_value(SecretId=secret_arn)
        _cached_webhook_secret = response.get("SecretString") or ""
    return _cached_webhook_secret


def _verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> None:
    """Verify X-Hub-Signature-256 using webhook secret."""
    if not signature_header or not signature_header.startswith("sha256="):
        raise PermissionError("Missing/invalid X-Hub-Signature-256")

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, received):
        raise PermissionError("Invalid signature")


def _is_pr_issue_comment(payload: dict) -> bool:
    """Check if this is a comment on a PR (not a regular issue)."""
    issue = payload.get("issue") or {}
    return "pull_request" in issue


def _is_quartz_review_command(payload: dict) -> bool:
    """Check if the comment is a /quartz review command."""
    comment = payload.get("comment") or {}
    body = (comment.get("body") or "").strip()
    return body.startswith("/quartz review")


def handler(event, context):
    """Lambda handler for GitHub webhook events."""
    # Normalize headers to lowercase
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    github_event = headers.get("x-github-event", "")
    delivery_id = headers.get("x-github-delivery", "")
    signature = headers.get("x-hub-signature-256")

    # API Gateway may base64 encode the body
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(body)
    else:
        raw_body = body.encode("utf-8")

    # Verify webhook signature
    try:
        secret = _get_webhook_secret()
        _verify_signature(raw_body, signature, secret)
    except PermissionError as error:
        print(f"[Receiver] Signature verification failed: {error}")
        return {"statusCode": 401, "body": json.dumps({"ok": False, "error": str(error)})}
    except Exception as error:
        print(f"[Receiver] Error getting webhook secret: {error}")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": "Internal error"})}

    payload = json.loads(raw_body.decode("utf-8") or "{}")

    # Handle GitHub ping event
    if github_event == "ping":
        print("[Receiver] Received ping event")
        return {"statusCode": 200, "body": json.dumps({"ok": True, "msg": "pong"})}

    triggered = False

    # Check for /quartz review command on PR
    if github_event == "issue_comment" and payload.get("action") == "created":
        if _is_pr_issue_comment(payload) and _is_quartz_review_command(payload):
            owner = payload["repository"]["owner"]["login"]
            repo = payload["repository"]["name"]
            pr_number = int(payload["issue"]["number"])
            installation_id = int(payload["installation"]["id"])

            # Extract trigger user info
            comment_user = payload.get("comment", {}).get("user", {})
            triggered_by = comment_user.get("login")
            triggered_by_id = comment_user.get("id")

            job = {
                "owner": owner,
                "repo": repo,
                "pr_number": pr_number,
                "installation_id": installation_id,
                "delivery_id": delivery_id,
                "triggered_by": triggered_by,
                "triggered_by_id": triggered_by_id,
            }

            queue_url = os.environ["REVIEW_QUEUE_URL"]
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(job),
            )

            print(f"[Receiver] Enqueued review job: {owner}/{repo}#{pr_number} (triggered by @{triggered_by})")
            triggered = True

    return {"statusCode": 200, "body": json.dumps({"ok": True, "triggered": triggered})}
