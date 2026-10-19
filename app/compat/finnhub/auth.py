"""Finnhub-identical authentication and rate limiting.

Measured against Finnhub (tests/contract/contract.json):

  no token        401 {"error": "Please use an API key."}
  unknown token   401 {"error": "Invalid API key"}
  over the limit  429 {"error": "Too many requests. Please try again later."}

The key is accepted on `?token=` or the `X-Finnhub-Token` header, and every
response carries `x-ratelimit-limit`, `x-ratelimit-remaining` and
`x-ratelimit-reset` (epoch seconds). There is no `Retry-After`, because
Finnhub sends none.
"""

import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings

NO_KEY = {"error": "Please use an API key."}
BAD_KEY = {"error": "Invalid API key"}
TOO_MANY = {"error": "Too many requests. Please try again later."}


class MinuteWindow:
    """Fixed one-minute windows per key, the shape Finnhub's headers describe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def hit(self, key: str, limit: int) -> tuple[bool, int, int]:
        """Count one request. Returns (allowed, remaining, reset_epoch)."""
        now = int(time.time())
        window_start = now - now % 60
        reset = window_start + 60
        with self._lock:
            start, used = self._windows.get(key, (window_start, 0))
            if start != window_start:
                start, used = window_start, 0
            if used >= limit:
                return False, 0, reset
            used += 1
            self._windows[key] = (start, used)
        return True, limit - used, reset


window = MinuteWindow()


def _token(request: Request) -> str | None:
    return request.query_params.get("token") or request.headers.get("x-finnhub-token")


async def finnhub_auth_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api/v1/"):
        return await call_next(request)

    token = _token(request)
    if not token:
        return JSONResponse(NO_KEY, status_code=401)
    if token not in settings.api_key_set:
        return JSONResponse(BAD_KEY, status_code=401)

    limit = settings.rate_limit_per_minute
    allowed, remaining, reset = window.hit(token, limit)
    headers = {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(reset),
    }
    if not allowed:
        return JSONResponse(TOO_MANY, status_code=429, headers=headers)

    response = await call_next(request)
    for name, value in headers.items():
        response.headers[name] = value
    return response
