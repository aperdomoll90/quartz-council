from __future__ import annotations

from dataclasses import dataclass
import httpx


@dataclass(frozen=True)
class GitHubClient:
    token: str

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
        }

    async def get_json(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self.headers())
            r.raise_for_status()
            return r.json()

    async def post_json(self, url: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=self.headers(), json=body)
            r.raise_for_status()
            return r.json()
