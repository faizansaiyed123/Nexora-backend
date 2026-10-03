"""
Redis-backed fixed-window rate limiting and atomic one-time-token/session consumption.

Rate limiting uses an INCR + TTL fixed window and intentionally fails open when
Redis is unavailable, preserving the existing runtime behavior.
"""

import logging
import time
import threading
from typing import Optional
from backend.core.config import get_settings
from backend.core.exceptions import RateLimitException

logger = logging.getLogger(__name__)
settings = get_settings()

_redis_client = None
_fallback_counts: dict[str, tuple[int, float]] = {}
_fallback_lock = threading.Lock()
_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


def _fallback_check(key: str, max_requests: int, window_seconds: int) -> None:
    now = time.monotonic()
    with _fallback_lock:
        count, window_start = _fallback_counts.get(key, (0, now))
        if now - window_start >= window_seconds:
            count, window_start = 0, now
        count += 1
        _fallback_counts[key] = (count, window_start)
        if count > max_requests:
            retry_after = max(1, int(window_seconds - (now - window_start)))
            raise RateLimitException(
                message=f"Rate limit exceeded. Please try again in {retry_after} seconds.",
                code="RATE_LIMIT_EXCEEDED",
            )


async def get_redis_client():
    global _redis_client
    if _redis_client is None:
        try:
            import redis.asyncio as aioredis
            _redis_client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
        except Exception as e:
            logger.warning(f"Could not connect to Redis: {e}")
            return None
    return _redis_client


class DistributedRateLimiter:
    @classmethod
    async def check_rate_limit(
        cls,
        key_prefix: str,
        identifier: str,
        max_requests: int,
        window_seconds: int,
    ) -> None:
        if not settings.rate_limit_enabled:
            return

        key = f"rl:{key_prefix}:{identifier}"
        redis = await get_redis_client()
        if not redis:
            _fallback_check(key, max_requests, window_seconds)
            return

        try:
            current_count = await redis.eval(
                _RATE_LIMIT_LUA,
                1,
                key,
                window_seconds,
            )
            if int(current_count) > max_requests:
                ttl = await redis.ttl(key)
                raise RateLimitException(
                    message=f"Rate limit exceeded. Please try again in {max(ttl, 1)} seconds.",
                    code="RATE_LIMIT_EXCEEDED",
                )
        except RateLimitException:
            raise
        except Exception as e:
            logger.error(f"Redis rate limit error: {e}")
            _fallback_check(key, max_requests, window_seconds)


class TokenSessionStore:
    @classmethod
    async def store_email_verification_token(cls, token_hash: str, user_id: str, email: str, ttl_seconds: int = 86400) -> None:
        redis = await get_redis_client()
        if not redis:
            raise RuntimeError("Redis is required for email verification tokens.")
        try:
            import json
            payload = json.dumps({"user_id": user_id, "email": email})
            await redis.setex(f"email_verify:{token_hash}", ttl_seconds, payload)
        except Exception as e:
            logger.error(f"Failed to store email verification token: {e}")
            raise

    @classmethod
    async def get_and_consume_email_verification_token(cls, token_hash: str) -> Optional[dict]:
        redis = await get_redis_client()
        if not redis:
            return None
        try:
            import json
            key = f"email_verify:{token_hash}"
            raw = await redis.eval(
                "local value = redis.call('GET', KEYS[1]); if value then redis.call('DEL', KEYS[1]); end; return value",
                1,
                key,
            )
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.error(f"Failed to consume email verification token: {e}")
        return None

    @classmethod
    async def store_password_reset_token(cls, token_hash: str, user_id: str, email: str, ttl_seconds: int = 900) -> None:
        redis = await get_redis_client()
        if not redis:
            raise RuntimeError("Redis is required for password reset tokens.")
        try:
            import json
            payload = json.dumps({"user_id": user_id, "email": email})
            await redis.setex(f"pwd_reset:{token_hash}", ttl_seconds, payload)
        except Exception as e:
            logger.error(f"Failed to store password reset token: {e}")
            raise

    @classmethod
    async def get_and_consume_password_reset_token(cls, token_hash: str) -> Optional[dict]:
        redis = await get_redis_client()
        if redis:
            try:
                import json
                key = f"pwd_reset:{token_hash}"
                raw = await redis.eval(
                    "local value = redis.call('GET', KEYS[1]); if value then redis.call('DEL', KEYS[1]); end; return value",
                    1,
                    key,
                )
                if raw:
                    return json.loads(raw)
            except Exception as e:
                logger.error(f"Failed to consume password reset token: {e}")
        return None

    @classmethod
    async def store_refresh_session(
        cls,
        user_id: str,
        session_id: str,
        token_hash: str,
        ttl_seconds: int = 604800,
    ) -> None:
        redis = await get_redis_client()
        if not redis:
            raise RuntimeError("Redis is required for refresh sessions.")
        try:
            await redis.setex(f"refresh:{user_id}:{session_id}", ttl_seconds, token_hash)
        except Exception as e:
            logger.error(f"Failed to store refresh session: {e}")
            raise

    @classmethod
    async def validate_and_rotate_refresh_session(
        cls,
        user_id: str,
        session_id: str,
        provided_token_hash: str,
        new_token_hash: str,
        ttl_seconds: int = 604800,
    ) -> bool:
        redis = await get_redis_client()
        if not redis:
            return False

        try:
            key = f"refresh:{user_id}:{session_id}"
            result = await redis.eval(
                """
                local current = redis.call('GET', KEYS[1])
                if not current then
                    return 0
                end
                if current ~= ARGV[1] then
                    redis.call('DEL', KEYS[1])
                    return -1
                end
                redis.call('SETEX', KEYS[1], ARGV[2], ARGV[3])
                return 1
                """,
                1,
                key,
                provided_token_hash,
                ttl_seconds,
                new_token_hash,
            )
            if result == 1:
                return True
            if result == -1:
                logger.warning(f"Refresh token reuse detected for user {user_id}! Revoking session {session_id}.")
            return False
        except Exception as e:
            logger.error(f"Failed to rotate refresh session: {e}")
            return False

    @classmethod
    async def revoke_user_sessions(cls, user_id: str) -> None:
        redis = await get_redis_client()
        if not redis:
            raise RuntimeError("Redis is required to revoke refresh sessions.")
        try:
            pattern = f"refresh:{user_id}:*"
            keys = await redis.keys(pattern)
            if keys:
                await redis.delete(*keys)
        except Exception as e:
            logger.error(f"Failed to revoke sessions: {e}")
            raise
