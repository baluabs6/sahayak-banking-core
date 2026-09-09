"""
Redis connectivity + reusable helpers.

Two responsibilities in this app:
  1. Cache: short-TTL caching of expensive/repeatable results — credit
     score computations and RAG/LLM answers — so identical requests
     within the TTL window skip recomputation and, importantly, skip a
     paid LLM API call.
  2. Rate limiting: a fixed-window counter protecting the LLM-backed
     `/assistant/ask` route from abuse (each paid API call costs money
     and has latency; this is the app-level throttle that sits in front
     of, not instead of, API Gateway's own throttling).

Redis is a cache, not a system of record — every value stored here is
reconstructible from Postgres/Mongo/the LLM itself. If Redis is down or
unreachable, callers should degrade gracefully (recompute / call the
LLM directly) rather than fail the request; the helpers below reflect
that with try/except-and-return-None semantics.
"""
import hashlib
import json
from typing import Any

from redis import asyncio as aioredis

from app.config import get_settings

settings = get_settings()

_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True, max_connections=20)
    return _client


def make_cache_key(*parts: str) -> str:
    """Builds a stable cache key from arbitrary parts, hashing any long/variable
    payloads (e.g. a full alt-data dict or a free-text query) so keys stay short
    and collision-safe."""
    joined = "|".join(parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]
    return f"{settings.app_name}:{digest}"


async def cache_get_json(key: str) -> Any | None:
    try:
        redis = get_redis_client()
        raw = await redis.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None  # cache miss on any Redis error — never block the request


async def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    try:
        redis = get_redis_client()
        await redis.set(key, json.dumps(value), ex=ttl_seconds)
    except Exception:
        pass  # best-effort cache write


async def cache_delete(key: str) -> None:
    try:
        redis = get_redis_client()
        await redis.delete(key)
    except Exception:
        pass


async def check_rate_limit(identity: str, route: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Fixed-window rate limiter. Returns (allowed, current_count).
    On Redis failure, fails OPEN (allowed=True) — an unreachable cache
    should not take down the API; API Gateway throttling is the backstop."""
    key = f"{settings.app_name}:ratelimit:{route}:{identity}"
    try:
        redis = get_redis_client()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_seconds)
        return count <= limit, count
    except Exception:
        return True, 0
