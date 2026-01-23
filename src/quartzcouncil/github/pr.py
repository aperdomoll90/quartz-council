from __future__ import annotations

import httpx


async def fetch_pr_files(owner: str, repo: str, pr_number: int, token: str) -> list[dict]:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()
