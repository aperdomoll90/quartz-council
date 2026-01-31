"""
QuartzCouncil Worker Lambda

Processes review jobs from SQS:
1. Fetches secrets from AWS Secrets Manager
2. Gets installation token from GitHub
3. Fetches PR files and runs the review council
4. Publishes review to GitHub
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import traceback

import boto3

# These imports come from the quartzcouncil layer
from quartzcouncil.agents.quartz import review_council
from quartzcouncil.core.pr_models import PullRequestInput, PullRequestFile
from quartzcouncil.github.auth import get_installation_token
from quartzcouncil.github.pr import fetch_pr_files
from quartzcouncil.github.client.github_client import GitHubClient
from quartzcouncil.github.client.pr_api import fetch_pr_head_sha, find_existing_quartz_review, post_issue_comment
from quartzcouncil.github.client.review_publisher import create_pr_review
from quartzcouncil.github.client.config_api import fetch_quartzcouncil_config

secrets_client = boto3.client("secretsmanager")
dynamodb_client = boto3.client("dynamodb")

# Cache secrets to avoid repeated API calls
_secrets_cache: dict[str, str] = {}


def _get_secret(secret_arn: str) -> str:
    """Fetch secret from Secrets Manager with caching."""
    if secret_arn not in _secrets_cache:
        response = secrets_client.get_secret_value(SecretId=secret_arn)
        _secrets_cache[secret_arn] = response.get("SecretString") or ""
    return _secrets_cache[secret_arn]


def _load_secrets_to_env() -> None:
    """Load all required secrets into environment variables."""
    os.environ["OPENAI_API_KEY"] = _get_secret(os.environ["OPENAI_API_KEY_ARN"])
    os.environ["GITHUB_APP_ID"] = _get_secret(os.environ["GITHUB_APP_ID_ARN"])

    # For the private key, we store the PEM content directly in the secret
    # and set it as an env var that auth.py can read
    private_key_pem = _get_secret(os.environ["GITHUB_PRIVATE_KEY_ARN"])
    os.environ["GITHUB_PRIVATE_KEY_PEM"] = private_key_pem


def _already_processed(delivery_id: str) -> bool:
    """Check if this delivery has already been processed (idempotency)."""
    if not delivery_id:
        return False

    table_name = os.environ.get("DELIVERY_TABLE")
    if not table_name:
        return False

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={"delivery_id": {"S": delivery_id}},
        ConsistentRead=True,
    )
    return "Item" in response


def _mark_processed(delivery_id: str, ttl_seconds: int = 3600) -> None:
    """Mark delivery as processed with TTL for cleanup."""
    if not delivery_id:
        return

    table_name = os.environ.get("DELIVERY_TABLE")
    if not table_name:
        return

    ttl_timestamp = int(time.time()) + ttl_seconds
    try:
        dynamodb_client.put_item(
            TableName=table_name,
            Item={
                "delivery_id": {"S": delivery_id},
                "ttl": {"N": str(ttl_timestamp)},
            },
            ConditionExpression="attribute_not_exists(delivery_id)",
        )
    except dynamodb_client.exceptions.ConditionalCheckFailedException:
        # Already exists - that's fine
        pass


async def _process_review(job: dict) -> dict:
    """Run the full review pipeline."""
    owner = job["owner"]
    repo = job["repo"]
    pr_number = int(job["pr_number"])
    installation_id = int(job["installation_id"])
    triggered_by = job.get("triggered_by")
    triggered_by_id = job.get("triggered_by_id")

    print(f"[Worker] Processing review: {owner}/{repo}#{pr_number} (triggered by @{triggered_by})")

    # Get installation token
    token = await get_installation_token(installation_id)
    github_client = GitHubClient(token)

    # Fetch head SHA for idempotency check
    head_sha = await fetch_pr_head_sha(owner, repo, pr_number, github_client)

    # Check if we already reviewed this commit (GitHub-based idempotency)
    idempotency_enabled = os.getenv("QUARTZ_IDEMPOTENCY_CHECK", "true").lower() == "true"
    if idempotency_enabled:
        existing_review = await find_existing_quartz_review(owner, repo, pr_number, head_sha, github_client)
        if existing_review:
            print(f"[Worker] Already reviewed commit {head_sha[:7]}")
            feedback_message = (
                f"A QuartzCouncil review already exists for this commit (`{head_sha[:7]}`).\n\n"
                f"[View existing review]({existing_review.html_url})\n\n"
                f"_Push new commits to trigger a fresh review._"
            )
            await post_issue_comment(owner, repo, pr_number, feedback_message, github_client)
            return {
                "skipped": True,
                "reason": "already_reviewed",
                "existing_review_url": existing_review.html_url,
            }

    # Fetch PR files
    github_files = await fetch_pr_files(owner, repo, pr_number, token)

    files: list[PullRequestFile] = []
    for github_file in github_files:
        patch = github_file.get("patch")
        if not patch:
            continue
        files.append(PullRequestFile(filename=github_file["filename"], patch=patch))

    if not files:
        print(f"[Worker] No patchable files found for {owner}/{repo}#{pr_number}")
        return {"skipped": True, "reason": "no_files"}

    # Fetch PR title for context
    pr_data = await github_client.get_json(f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}")
    title = pr_data.get("title", "")

    pr_input = PullRequestInput(number=pr_number, title=title, files=files)

    # Fetch repo config for Chalcedony agent (optional)
    repo_config = await fetch_quartzcouncil_config(owner, repo, head_sha, github_client)

    print(f"[Worker] Running council on {len(files)} patched files...")
    review = await review_council(
        pr_input,
        cfg=repo_config,
        triggered_by=triggered_by,
        triggered_by_id=triggered_by_id,
    )

    print(f"[Worker] Council complete: {len(review.comments)} comments")

    # Log token usage
    if review.meta.total_tokens > 0:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        cost = review.meta.total_cost_usd(model)
        print(f"[Worker] Token usage: {review.meta.total_tokens:,} tokens (~${cost:.4f})")

    # Publish review to GitHub
    published = await create_pr_review(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        commit_id=head_sha,
        summary_md=review.summary,
        comments=review.comments,
        files=files,
        gh=github_client,
    )

    review_url = published.get("html_url") or published.get("url")
    print(f"[Worker] Published review: {review_url}")

    return {
        "reviewed_files": len(files),
        "comments": len(review.comments),
        "review_url": review_url,
    }


def handler(event, context):
    """Lambda handler for SQS messages."""
    # Load secrets into environment on cold start
    _load_secrets_to_env()

    record = event["Records"][0]
    job = json.loads(record["body"])
    delivery_id = job.get("delivery_id", "")

    print(f"[Worker] Received job: {json.dumps(job)}")

    # Check DynamoDB idempotency (prevents duplicate processing from SQS retries)
    if delivery_id and _already_processed(delivery_id):
        print(f"[Worker] Skipping duplicate delivery: {delivery_id}")
        return {"ok": True, "skipped": True, "reason": "duplicate_delivery"}

    try:
        result = asyncio.run(_process_review(job))

        # Mark as processed after successful completion
        if delivery_id:
            _mark_processed(delivery_id)

        return {"ok": True, **result}

    except Exception as error:
        print(f"[Worker] Error processing review: {error}")
        traceback.print_exc()

        # Try to post error feedback to PR
        try:
            owner = job["owner"]
            repo = job["repo"]
            pr_number = int(job["pr_number"])
            installation_id = int(job["installation_id"])

            async def post_error():
                token = await get_installation_token(installation_id)
                github_client = GitHubClient(token)
                error_message = (
                    f"QuartzCouncil review failed.\n\n"
                    f"```\n{str(error)}\n```\n\n"
                    f"_Please try again or report this issue._"
                )
                await post_issue_comment(owner, repo, pr_number, error_message, github_client)

            asyncio.run(post_error())
        except Exception:
            pass  # Don't fail if error comment fails

        # Re-raise to let Lambda/SQS handle retry
        raise
