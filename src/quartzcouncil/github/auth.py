from __future__ import annotations

import os
import time

import httpx
import jwt


def _read_private_key() -> str:
    path = os.getenv("GITHUB_PRIVATE_KEY_PATH", "")
    if not path:
        raise RuntimeError("Missing GITHUB_PRIVATE_KEY_PATH")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def create_app_jwt() -> str:
    app_id = os.getenv("GITHUB_APP_ID", "")
    if not app_id:
        raise RuntimeError("Missing GITHUB_APP_ID")

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 600,  # ~10 min
        "iss": app_id,
    }

    private_key = _read_private_key()
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    app_jwt = create_app_jwt()
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json()["token"]
