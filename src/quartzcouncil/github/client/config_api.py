"""
GitHub API functions for fetching .quartzcouncil.yml config files.
"""
from __future__ import annotations

import base64
import httpx
import yaml
from pydantic import ValidationError

from quartzcouncil.core.config_models import QuartzCouncilConfig
from quartzcouncil.github.client.github_client import GitHubClient


# Config file locations to try (in order)
CONFIG_PATHS = [
    ".quartzcouncil.yml",
    ".github/.quartzcouncil.yml",
]


async def fetch_quartzcouncil_config(
    owner: str,
    repo: str,
    ref: str,
    gh: GitHubClient,
) -> QuartzCouncilConfig | None:
    """
    Fetch and parse .quartzcouncil.yml from the repository.

    Attempts to load from:
    1. .quartzcouncil.yml (repo root)
    2. .github/.quartzcouncil.yml

    Args:
        owner: Repository owner
        repo: Repository name
        ref: Git ref (branch, tag, or SHA) - use PR head SHA for PR context
        gh: Authenticated GitHub client

    Returns:
        Parsed QuartzCouncilConfig if found and valid, None otherwise.
    """
    for config_path in CONFIG_PATHS:
        config = await _try_fetch_config(owner, repo, ref, config_path, gh)
        if config is not None:
            return config

    return None


async def _try_fetch_config(
    owner: str,
    repo: str,
    ref: str,
    path: str,
    gh: GitHubClient,
) -> QuartzCouncilConfig | None:
    """
    Attempt to fetch and parse config from a specific path.

    Returns None if file not found, invalid YAML, or validation fails.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=gh.headers())

            if response.status_code == 404:
                return None

            response.raise_for_status()
            data = response.json()

    except httpx.HTTPStatusError as http_error:
        print(f"[QuartzCouncil] ⚠️ Failed to fetch config from {path}: {http_error}")
        return None
    except Exception as fetch_error:
        print(f"[QuartzCouncil] ⚠️ Error fetching config from {path}: {fetch_error}")
        return None

    # GitHub returns content base64 encoded
    content_b64 = data.get("content", "")
    if not content_b64:
        print(f"[QuartzCouncil] ⚠️ Config file {path} has no content")
        return None

    try:
        content_bytes = base64.b64decode(content_b64)
        content_text = content_bytes.decode("utf-8")
    except Exception as decode_error:
        print(f"[QuartzCouncil] ⚠️ Failed to decode config from {path}: {decode_error}")
        return None

    # Parse YAML
    try:
        yaml_data = yaml.safe_load(content_text)
        if not yaml_data:
            print(f"[QuartzCouncil] ⚠️ Config file {path} is empty or invalid YAML")
            return None
    except yaml.YAMLError as yaml_error:
        print(f"[QuartzCouncil] ⚠️ Invalid YAML in {path}: {yaml_error}")
        return None

    # Validate with Pydantic
    try:
        config = QuartzCouncilConfig.model_validate(yaml_data)
        print(f"[QuartzCouncil] 📋 Loaded config from {path}")
        return config
    except ValidationError as validation_error:
        print(f"[QuartzCouncil] ⚠️ Invalid config structure in {path}: {validation_error}")
        return None
