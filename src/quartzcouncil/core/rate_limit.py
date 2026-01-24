from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""
    max_reviews_per_hour: int = 5
    window_seconds: int = 3600  # 1 hour


@dataclass
class RateLimiter:
    """
    Simple in-memory rate limiter per installation.

    Note: This is process-local and resets on restart.
    For production, use Redis or similar.
    """
    config: RateLimitConfig = field(default_factory=RateLimitConfig)
    _timestamps: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))

    def _clean_old_entries(self, installation_id: int) -> None:
        """Remove timestamps older than the window."""
        cutoff = time.time() - self.config.window_seconds
        self._timestamps[installation_id] = [
            ts for ts in self._timestamps[installation_id]
            if ts > cutoff
        ]

    def check_rate_limit(self, installation_id: int) -> tuple[bool, int]:
        """
        Check if a review is allowed for this installation.

        Returns:
            Tuple of (allowed, remaining_count)
        """
        self._clean_old_entries(installation_id)
        current_count = len(self._timestamps[installation_id])
        remaining = max(0, self.config.max_reviews_per_hour - current_count)
        allowed = current_count < self.config.max_reviews_per_hour
        return (allowed, remaining)

    def record_review(self, installation_id: int) -> None:
        """Record that a review was performed."""
        self._timestamps[installation_id].append(time.time())

    def get_retry_after_seconds(self, installation_id: int) -> int:
        """Get seconds until next review is allowed."""
        self._clean_old_entries(installation_id)
        if not self._timestamps[installation_id]:
            return 0
        oldest = min(self._timestamps[installation_id])
        retry_after = int(oldest + self.config.window_seconds - time.time())
        return max(0, retry_after)


# Global rate limiter instance
_rate_limiter = RateLimiter()


def check_rate_limit(installation_id: int) -> tuple[bool, int]:
    """Check if review is allowed. Returns (allowed, remaining)."""
    return _rate_limiter.check_rate_limit(installation_id)


def record_review(installation_id: int) -> None:
    """Record that a review was performed."""
    _rate_limiter.record_review(installation_id)


def get_retry_after(installation_id: int) -> int:
    """Get seconds until next review is allowed."""
    return _rate_limiter.get_retry_after_seconds(installation_id)
