"""
Redis-backed fixed-window rate limiting and atomic one-time-token/session consumption.

Rate limiting uses an atomic Redis Lua counter and a bounded process-local fallback
when Redis is unavailable so authentication-sensitive endpoints remain throttled.
"""

import logging
import time
from typing import Optional
from backend.core.config import get_settings
from backend.core.exceptions import RateLimitException

logger = logging.getLogger(__name__)
settings = get_settings()

_redis_client = None
_LOCAL_WINDOWS: dict[str, tuple[float, int]] = {}
_LOCAL_MAX_KEYS = 10000
_AUTH_VERSION_CACHE_TTL = 5


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

        redis = await get_redis_client()
        key = f"rl:{key_prefix}:{identifier}"
        if not redis:
            cls._check_local_fallback(key, max_requests, window_seconds)
            return
        try:
            current_count = int(await redis.eval(
                """
                local current = redis.call('INCR', KEYS[1])
                if current == 1 then
                    redis.call('EXPIRE', KEYS[1], ARGV[1])
                end
                return current
                """,
                1,
                key,
                window_seconds,
            ))

            if current_count > max_requests:
                ttl = await redis.ttl(key)
                raise RateLimitException(
                    message=f"Rate limit exceeded. Please try again in {max(ttl, 1)} seconds.",
                    code="RATE_LIMIT_EXCEEDED",
                )
        except RateLimitException:
            raise
        except Exception as e:
            logger.error(f"Redis rate limit error: {e}")
            cls._check_local_fallback(key, max_requests, window_seconds)

    @classmethod
    def _check_local_fallback(cls, key: str, max_requests: int, window_seconds: int) -> None:
        now = time.monotonic()
        start, count = _LOCAL_WINDOWS.get(key, (now, 0))
        if now - start >= window_seconds:
            start, count = now, 0
        count += 1
        _LOCAL_WINDOWS[key] = (start, count)
        if len(_LOCAL_WINDOWS) > _LOCAL_MAX_KEYS:
            cutoff = now - max(window_seconds, 60)
            for candidate, (candidate_start, _) in list(_LOCAL_WINDOWS.items()):
                if candidate_start < cutoff:
                    _LOCAL_WINDOWS.pop(candidate, None)
        if count > max_requests:
            retry_after = max(1, int(window_seconds - (now - start)))
            raise RateLimitException(
                message=f"Rate limit exceeded. Please try again in {retry_after} seconds.",
                code="RATE_LIMIT_EXCEEDED",
            )


class AuthorizationStateStore:
    @classmethod
    async def get_cached_version(cls, user_id: str) -> Optional[int]:
        redis = await get_redis_client()
        if not redis:
            return None
        try:
            value = await redis.get(f"authver:{user_id}")
            return int(value) if value is not None else None
        except Exception as exc:
            logger.error("Authorization version lookup failed: %s", exc)
            return None

    @classmethod
    async def set_version(cls, user_id: str, version: int) -> None:
        redis = await get_redis_client()
        if not redis:
            return
        try:
            await redis.setex(
                f"authver:{user_id}",
                _AUTH_VERSION_CACHE_TTL,
                int(version),
            )
        except Exception as exc:
            logger.error("Authorization version cache update failed: %s", exc)


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
