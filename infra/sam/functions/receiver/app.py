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


def _parse_quartz_command(payload: dict) -> dict | None:
    """
    Parse /quartz command from comment.

    Supported formats:
    - /quartz review          → Run all agents (default)
    - /quartz amethyst        → Run only Amethyst (TypeScript)
    - /quartz citrine         → Run only Citrine (React/Next)
    - /quartz chalcedony      → Run only Chalcedony (conventions)
    - /quartz amethyst citrine → Run multiple specific agents

    Returns dict with 'agents' key (list of agent names) or None if not a quartz command.
    """
    comment = payload.get("comment") or {}
    body = (comment.get("body") or "").strip().lower()

    if not body.startswith("/quartz"):
        return None

    # Parse the command parts
    parts = body.split()

    # /quartz alone or /quartz review → run all agents
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "review"):
        return {"agents": None}  # None means all agents

    # Parse specific agent names
    valid_agents = {"amethyst", "citrine", "chalcedony"}
    requested_agents = []

    for part in parts[1:]:
        if part == "review":
            continue  # Skip "review" keyword
        if part in valid_agents:
            requested_agents.append(part)

    # If we found valid agents, return them; otherwise treat as "run all"
    if requested_agents:
        return {"agents": requested_agents}

    return {"agents": None}


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

    # Check for /quartz command on PR
    if github_event == "issue_comment" and payload.get("action") == "created":
        command = _parse_quartz_command(payload) if _is_pr_issue_comment(payload) else None

        if command is not None:
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
                "agents": command["agents"],  # None = all, or list of specific agents
            }

            queue_url = os.environ["REVIEW_QUEUE_URL"]
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(job),
            )

            agents_str = ", ".join(command["agents"]) if command["agents"] else "all"
            print(f"[Receiver] Enqueued review job: {owner}/{repo}#{pr_number} (triggered by @{triggered_by}, agents: {agents_str})")
            triggered = True

    return {"statusCode": 200, "body": json.dumps({"ok": True, "triggered": triggered})}
