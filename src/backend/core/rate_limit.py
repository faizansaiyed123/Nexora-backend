"""
Distributed Redis-backed sliding-window rate limiting and abuse prevention.
"""

import logging
from typing import Optional
from backend.core.config import get_settings
from backend.core.exceptions import RateLimitException

logger = logging.getLogger(__name__)
settings = get_settings()

_redis_client = None


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
        if not redis:
            return

        key = f"rl:{key_prefix}:{identifier}"
        try:
            current_count = await redis.incr(key)
            if current_count == 1:
                await redis.expire(key, window_seconds)

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
            return


class TokenSessionStore:
    @classmethod
    async def store_email_verification_token(cls, token_hash: str, user_id: str, email: str, ttl_seconds: int = 86400) -> None:
        redis = await get_redis_client()
        if redis:
            try:
                import json
                payload = json.dumps({"user_id": user_id, "email": email})
                await redis.setex(f"email_verify:{token_hash}", ttl_seconds, payload)
            except Exception as e:
                logger.error(f"Failed to store email verification token: {e}")

    @classmethod
    async def get_and_consume_email_verification_token(cls, token_hash: str) -> Optional[dict]:
        redis = await get_redis_client()
        if redis:
            try:
                import json
                key = f"email_verify:{token_hash}"
                raw = await redis.get(key)
                if raw:
                    await redis.delete(key)
                    return json.loads(raw)
            except Exception as e:
                logger.error(f"Failed to consume email verification token: {e}")
        return None

    @classmethod
    async def store_password_reset_token(cls, token_hash: str, user_id: str, email: str, ttl_seconds: int = 900) -> None:
        redis = await get_redis_client()
        if redis:
            try:
                import json
                payload = json.dumps({"user_id": user_id, "email": email})
                await redis.setex(f"pwd_reset:{token_hash}", ttl_seconds, payload)
            except Exception as e:
                logger.error(f"Failed to store password reset token: {e}")

    @classmethod
    async def get_and_consume_password_reset_token(cls, token_hash: str) -> Optional[dict]:
        redis = await get_redis_client()
        if redis:
            try:
                import json
                key = f"pwd_reset:{token_hash}"
                raw = await redis.get(key)
                if raw:
                    await redis.delete(key)
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
        if redis:
            try:
                await redis.setex(f"refresh:{user_id}:{session_id}", ttl_seconds, token_hash)
            except Exception as e:
                logger.error(f"Failed to store refresh session: {e}")

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
            return True

        try:
            key = f"refresh:{user_id}:{session_id}"
            stored_hash = await redis.get(key)
            if not stored_hash:
                return False

            if stored_hash != provided_token_hash:
                logger.warning(f"Refresh token reuse detected for user {user_id}! Revoking session {session_id}.")
                await redis.delete(key)
                return False

            await redis.setex(key, ttl_seconds, new_token_hash)
            return True
        except Exception as e:
            logger.error(f"Failed to rotate refresh session: {e}")
            return False

    @classmethod
    async def revoke_user_sessions(cls, user_id: str) -> None:
        redis = await get_redis_client()
        if redis:
            try:
                pattern = f"refresh:{user_id}:*"
                keys = await redis.keys(pattern)
                if keys:
                    await redis.delete(*keys)
            except Exception as e:
                logger.error(f"Failed to revoke sessions: {e}")
