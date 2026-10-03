"""Concurrency-focused tests for Redis-backed auth token state."""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from backend.core.rate_limit import DistributedRateLimiter, TokenSessionStore, get_redis_client
from backend.core.exceptions import RateLimitException


@pytest_asyncio.fixture
async def redis_client():
    redis = await get_redis_client()
    if redis is None:
        pytest.skip("Redis client is unavailable")
    try:
        await redis.ping()
    except Exception:
        pytest.skip("Redis server is unavailable")
    yield redis


@pytest.mark.asyncio
async def test_refresh_rotation_is_atomic_under_concurrency(redis_client):
    user_id = str(uuid.uuid4())
    session_id = uuid.uuid4().hex
    old_hash = uuid.uuid4().hex
    key = f"refresh:{user_id}:{session_id}"

    await TokenSessionStore.store_refresh_session(
        user_id=user_id,
        session_id=session_id,
        token_hash=old_hash,
        ttl_seconds=60,
    )

    try:
        results = await asyncio.gather(
            *[
                TokenSessionStore.validate_and_rotate_refresh_session(
                    user_id=user_id,
                    session_id=session_id,
                    provided_token_hash=old_hash,
                    new_token_hash=f"new-{index}-{uuid.uuid4().hex}",
                    ttl_seconds=60,
                )
                for index in range(20)
            ]
        )

        assert results.count(True) == 1
        assert results.count(False) == 19
        assert await redis_client.get(key) is None
    finally:
        await redis_client.delete(key)


@pytest.mark.asyncio
async def test_refresh_rotation_remains_sequential_and_reuse_is_rejected(redis_client):
    user_id = str(uuid.uuid4())
    session_id = uuid.uuid4().hex
    old_hash = uuid.uuid4().hex
    new_hash = uuid.uuid4().hex
    next_hash = uuid.uuid4().hex
    key = f"refresh:{user_id}:{session_id}"

    await TokenSessionStore.store_refresh_session(
        user_id=user_id,
        session_id=session_id,
        token_hash=old_hash,
        ttl_seconds=60,
    )

    try:
        assert await TokenSessionStore.validate_and_rotate_refresh_session(
            user_id=user_id,
            session_id=session_id,
            provided_token_hash=old_hash,
            new_token_hash=new_hash,
            ttl_seconds=60,
        ) is True
        assert await TokenSessionStore.validate_and_rotate_refresh_session(
            user_id=user_id,
            session_id=session_id,
            provided_token_hash=new_hash,
            new_token_hash=next_hash,
            ttl_seconds=60,
        ) is True
        assert await TokenSessionStore.validate_and_rotate_refresh_session(
            user_id=user_id,
            session_id=session_id,
            provided_token_hash=old_hash,
            new_token_hash=uuid.uuid4().hex,
            ttl_seconds=60,
        ) is False
        assert await redis_client.get(key) is None
    finally:
        await redis_client.delete(key)


@pytest.mark.asyncio
async def test_email_verification_token_consumption_is_atomic(redis_client):
    token_hash = uuid.uuid4().hex
    key = f"email_verify:{token_hash}"
    await TokenSessionStore.store_email_verification_token(
        token_hash=token_hash,
        user_id=str(uuid.uuid4()),
        email="atomic@example.com",
        ttl_seconds=60,
    )

    try:
        results = await asyncio.gather(
            *[
                TokenSessionStore.get_and_consume_email_verification_token(token_hash)
                for _ in range(20)
            ]
        )
        assert sum(result is not None for result in results) == 1
        assert await redis_client.get(key) is None
    finally:
        await redis_client.delete(key)


@pytest.mark.asyncio
async def test_password_reset_token_consumption_is_atomic(redis_client):
    token_hash = uuid.uuid4().hex
    key = f"pwd_reset:{token_hash}"
    await TokenSessionStore.store_password_reset_token(
        token_hash=token_hash,
        user_id=str(uuid.uuid4()),
        email="atomic@example.com",
        ttl_seconds=60,
    )

    try:
        results = await asyncio.gather(
            *[
                TokenSessionStore.get_and_consume_password_reset_token(token_hash)
                for _ in range(20)
            ]
        )
        assert sum(result is not None for result in results) == 1
        assert await redis_client.get(key) is None
    finally:
        await redis_client.delete(key)


@pytest.mark.asyncio
async def test_rate_limiter_fallback_enforces_without_redis():
    identifier = str(uuid.uuid4())
    with patch(
        "backend.core.rate_limit.get_redis_client",
        new=AsyncMock(return_value=None),
    ):
        await DistributedRateLimiter.check_rate_limit(
            key_prefix="test",
            identifier=identifier,
            max_requests=1,
            window_seconds=60,
        )
        with pytest.raises(RateLimitException):
            await DistributedRateLimiter.check_rate_limit(
                key_prefix="test",
                identifier=identifier,
                max_requests=1,
                window_seconds=60,
            )

@pytest.mark.asyncio
async def test_refresh_rotation_preserves_session_expiration(redis_client):
    user_id = str(uuid.uuid4())
    session_id = uuid.uuid4().hex
    old_hash = uuid.uuid4().hex
    new_hash = uuid.uuid4().hex
    key = f"refresh:{user_id}:{session_id}"

    await TokenSessionStore.store_refresh_session(
        user_id=user_id,
        session_id=session_id,
        token_hash=old_hash,
        ttl_seconds=60,
    )

    try:
        assert await TokenSessionStore.validate_and_rotate_refresh_session(
            user_id=user_id,
            session_id=session_id,
            provided_token_hash=old_hash,
            new_token_hash=new_hash,
            ttl_seconds=60,
        ) is True
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= 60
    finally:
        await redis_client.delete(key)
