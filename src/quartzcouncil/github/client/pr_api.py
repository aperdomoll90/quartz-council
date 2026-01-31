from __future__ import annotations

from dataclasses import dataclass

from quartzcouncil.github.client.github_client import GitHubClient


async def fetch_pr(owner: str, repo: str, pr_number: int, gh: GitHubClient) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    return await gh.get_json(url)


async def fetch_pr_head_sha(owner: str, repo: str, pr_number: int, gh: GitHubClient) -> str:
    pr = await fetch_pr(owner, repo, pr_number, gh)
    return pr["head"]["sha"]


# =============================================================================
# PR REVIEWS
# =============================================================================

QUARTZ_REVIEW_MARKER = "## QuartzCouncil Review"


@dataclass
class ExistingReview:
    """Represents an existing QuartzCouncil review on a PR."""
    review_id: int
    commit_id: str
    html_url: str


async def fetch_pr_reviews(owner: str, repo: str, pr_number: int, gh: GitHubClient) -> list[dict]:
    """Fetch all reviews on a PR."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    return await gh.get_json(url)


async def find_existing_quartz_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    gh: GitHubClient,
) -> ExistingReview | None:
    """
    Check if a QuartzCouncil review already exists for the given commit.

    Returns the existing review info if found, None otherwise.
    """
    reviews = await fetch_pr_reviews(owner, repo, pr_number, gh)

    for review in reviews:
        review_body = review.get("body") or ""
        review_commit = review.get("commit_id") or ""

        # Check if this is a QuartzCouncil review for the same commit
        if QUARTZ_REVIEW_MARKER in review_body and review_commit == commit_sha:
            return ExistingReview(
                review_id=review["id"],
                commit_id=review_commit,
                html_url=review.get("html_url") or "",
            )

    return None


# =============================================================================
# ISSUE COMMENTS
# =============================================================================

async def post_issue_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    gh: GitHubClient,
) -> dict:
    """Post a comment on an issue or PR."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    return await gh.post_json(url, {"body": body})
