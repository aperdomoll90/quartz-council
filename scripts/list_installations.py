#!/usr/bin/env python3
"""List all installations of the QuartzCouncil GitHub App."""

import asyncio
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from quartzcouncil.github.auth import create_app_jwt
import httpx


async def list_installations():
    """Fetch and display all GitHub App installations."""
    jwt_token = create_app_jwt()

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.github.com/app/installations",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        installations = response.json()

    if not installations:
        print("No installations found.")
        return

    print(f"Found {len(installations)} installation(s):\n")
    print("-" * 60)

    for install in installations:
        account = install.get("account", {})
        account_type = account.get("type", "Unknown")
        login = account.get("login", "Unknown")
        install_id = install.get("id")
        created_at = install.get("created_at", "Unknown")

        print(f"  {account_type}: {login}")
        print(f"  Installation ID: {install_id}")
        print(f"  Created: {created_at}")

        # Show repository selection
        repo_selection = install.get("repository_selection", "unknown")
        if repo_selection == "all":
            print("  Repos: All repositories")
        else:
            print(f"  Repos: Selected repositories only")

        print("-" * 60)


if __name__ == "__main__":
    asyncio.run(list_installations())