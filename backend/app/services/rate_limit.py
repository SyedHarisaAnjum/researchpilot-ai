"""Sliding-window rate limits for starting jobs and signing in.

With REDIS_URL set, counts live in Redis, so every API process and server shares them.
Without it, they live in this process's memory, which suits a single local server.
"""

import logging
import math
import time
import uuid
from collections import defaultdict, deque

from app.config import get_settings

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int, code: str = "rate_limited"):
        super().__init__(message)
        self.retry_after = retry_after
        self.code = code


def _describe(seconds: int) -> str:
    return {60: "minute", 900: "15 minutes", 3600: "hour", 86400: "day"}.get(seconds, f"{seconds} seconds")


def _exceeded(what: str, limit: int, seconds: int, retry_after: int) -> RateLimitExceeded:
    return RateLimitExceeded(
        f"Too many {what}: the limit is {limit} per {_describe(seconds)}. Try again in {retry_after} seconds.",
        max(retry_after, 1),
    )


class MemoryLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def hit(self, key: str, windows: list[tuple[int, int]], what: str = "requests") -> None:
        """Record one request for `key`, or raise if any (limit, seconds) window is already full."""
        now = time.time()
        hits = self._hits[key]
        longest = max(seconds for _, seconds in windows)
        while hits and hits[0] <= now - longest:
            hits.popleft()
        for limit, seconds in windows:
            recent = [t for t in hits if t > now - seconds]
            if len(recent) >= limit:
                raise _exceeded(what, limit, seconds, math.ceil(recent[0] + seconds - now))
        hits.append(now)

    async def reset(self) -> None:
        self._hits.clear()


class RedisLimiter:
    """The same sliding windows, as one sorted set of timestamps per key."""

    PREFIX = "ratelimit:"

    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        self.redis = Redis.from_url(url, decode_responses=True)

    async def hit(self, key: str, windows: list[tuple[int, int]], what: str = "requests") -> None:
        now = time.time()
        name = self.PREFIX + key
        longest = max(seconds for _, seconds in windows)
        pipe = self.redis.pipeline(transaction=True)
        pipe.zremrangebyscore(name, 0, now - longest)
        for _, seconds in windows:
            pipe.zrangebyscore(name, now - seconds, "+inf", start=0, num=1, withscores=True)
            pipe.zcount(name, now - seconds, "+inf")
        results = await pipe.execute()
        for index, (limit, seconds) in enumerate(windows):
            oldest, count = results[1 + 2 * index], results[2 + 2 * index]
            if count >= limit:
                first = oldest[0][1] if oldest else now
                raise _exceeded(what, limit, seconds, math.ceil(first + seconds - now))
        pipe = self.redis.pipeline(transaction=True)
        pipe.zadd(name, {f"{now}:{uuid.uuid4().hex[:8]}": now})
        pipe.expire(name, longest + 60)
        await pipe.execute()

    async def reset(self) -> None:
        async for name in self.redis.scan_iter(f"{self.PREFIX}*"):
            await self.redis.delete(name)

    async def close(self) -> None:
        await self.redis.aclose()


_limiter: MemoryLimiter | RedisLimiter | None = None


def get_limiter() -> MemoryLimiter | RedisLimiter:
    global _limiter
    if _limiter is None:
        url = get_settings().redis_url
        _limiter = RedisLimiter(url) if url else MemoryLimiter()
    return _limiter


async def close_limiter() -> None:
    global _limiter
    if isinstance(_limiter, RedisLimiter):
        await _limiter.close()
    _limiter = None
