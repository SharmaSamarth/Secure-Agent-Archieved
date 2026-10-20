"""
rate_limiter.py — Per-user quota enforcement.

Primary path: Redis sliding-window counter. Redis is the right tool here
because rate limits must be shared across every process/replica serving
the app — an in-process counter would let a user get N requests per
Streamlit worker instead of N total.

Fallback: if Redis isn't reachable (e.g. you're following along locally
and haven't started a Redis container yet), we fall back to an in-memory
limiter so the app still runs — but this ONLY works correctly with a
single process, and resets on restart. It exists so this repo is runnable
without infra sitting in front of you; log a warning and swap in real
Redis before anything resembling production.
"""
import time
import threading
from collections import defaultdict, deque

from config import settings

try:
    import redis
    _redis_client = redis.from_url(settings.redis_url, socket_connect_timeout=0.5)
    _redis_client.ping()
    _REDIS_AVAILABLE = True
except Exception:
    _redis_client = None
    _REDIS_AVAILABLE = False


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded — retry after {retry_after_seconds:.0f}s")


# ---------------------------------------------------------------------------
# In-memory fallback (single-process only)
# ---------------------------------------------------------------------------
_local_lock = threading.Lock()
_local_hits: dict[str, deque] = defaultdict(deque)


def _check_local(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    with _local_lock:
        hits = _local_hits[key]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = window_seconds - (now - hits[0])
            raise RateLimitExceeded(retry_after)
        hits.append(now)


# ---------------------------------------------------------------------------
# Redis sliding-window counter (ZSET of timestamps, trimmed each call)
# ---------------------------------------------------------------------------
def _check_redis(key: str, limit: int, window_seconds: int) -> None:
    redis_key = f"ratelimit:{key}"
    now = time.time()
    window_start = now - window_seconds

    pipe = _redis_client.pipeline()
    pipe.zremrangebyscore(redis_key, 0, window_start)
    pipe.zcard(redis_key)
    pipe.zadd(redis_key, {str(now): now})
    pipe.expire(redis_key, window_seconds)
    _, count, _, _ = pipe.execute()

    if count >= limit:
        oldest = _redis_client.zrange(redis_key, 0, 0, withscores=True)
        retry_after = window_seconds - (now - oldest[0][1]) if oldest else window_seconds
        # Remove the entry we just optimistically added, since this request is rejected.
        _redis_client.zrem(redis_key, str(now))
        raise RateLimitExceeded(max(retry_after, 0))


def check_rate_limit(
    key: str,
    limit: int = settings.rate_limit_requests,
    window_seconds: int = settings.rate_limit_window_seconds,
) -> None:
    """
    Raises RateLimitExceeded if `key` (typically f"user:{user_id}") has
    made >= `limit` requests in the trailing `window_seconds`.
    """
    if _REDIS_AVAILABLE:
        _check_redis(key, limit, window_seconds)
    else:
        _check_local(key, limit, window_seconds)


def rate_limit_status() -> str:
    return "redis" if _REDIS_AVAILABLE else "in-memory (Redis unreachable — dev fallback)"
