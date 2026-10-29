"""A small sliding-window rate limiter for the endpoints worth guessing at.

Sign-in is the obvious target -- a password guesser makes thousands of
attempts -- and the AI assistant costs money per call. Both get a per-client
budget; everything else is unlimited. The windows live in memory, so on a
multi-node deployment each node enforces its own share, which is the right
order of magnitude without a shared store.
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, limit: int, window: float = 60.0) -> None:
        """Count a hit for ``key``; raise 429 once ``limit`` is exceeded."""
        if limit <= 0:
            return
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            retry = max(1, int(window - (now - hits[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Try again in %d seconds." % retry,
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)
        if len(self._hits) > 10_000:
            # Drop idle keys so a scan from many addresses cannot grow this forever.
            for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > window]:
                del self._hits[stale]


def client_key(request: Request) -> str:
    """The caller's address, preferring the proxy's view when there is one."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter
