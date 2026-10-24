"""Exercise Nexora rate limiting against Redis and its process-local fallback."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core import rate_limit
from backend.core.config import get_settings
from backend.core.exceptions import RateLimitException


@pytest.mark.asyncio
async def test_redis_rate_limit_is_atomic_under_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    redis_url = get_settings().redis_url
    try:
        from redis.asyncio import from_url
        client = from_url(redis_url, encoding="utf-8", decode_responses=True)
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis is not available for the distributed limiter test: {exc}")

    async def redis_client():
        return client

    monkeypatch.setattr(rate_limit, "get_redis_client", redis_client)
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    identifier = f"ci-atomic-{uuid.uuid4().hex}"
    key = f"rl:{'integration-atomic'}:{identifier}"
    await client.delete(key)

    try:
        results = await asyncio.gather(
            *[
                rate_limit.DistributedRateLimiter.check_rate_limit(
                    "integration-atomic", identifier, max_requests=5, window_seconds=60
                )
                for _ in range(20)
            ],
            return_exceptions=True,
        )

        passed = [result for result in results if result is None]
        limited = [result for result in results if isinstance(result, RateLimitException)]
        unexpected = [
            result for result in results
            if result is not None and not isinstance(result, RateLimitException)
        ]

        assert not unexpected, unexpected
        assert len(passed) == 5
        assert len(limited) == 15
        assert all(result.status_code == 429 for result in limited)
        assert all(result.retry_after_seconds >= 1 for result in limited)
        assert int(await client.get(key)) == 20
    finally:
        await client.delete(key)
        await client.aclose()


@pytest.mark.asyncio
async def test_local_fallback_still_limits_when_redis_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable_redis():
        return None

    monkeypatch.setattr(rate_limit, "get_redis_client", unavailable_redis)
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    identifier = f"ci-fallback-{uuid.uuid4().hex}"

    await rate_limit.DistributedRateLimiter.check_rate_limit(
        "integration-fallback", identifier, max_requests=2, window_seconds=60
    )
    await rate_limit.DistributedRateLimiter.check_rate_limit(
        "integration-fallback", identifier, max_requests=2, window_seconds=60
    )
    with pytest.raises(RateLimitException) as error:
        await rate_limit.DistributedRateLimiter.check_rate_limit(
            "integration-fallback", identifier, max_requests=2, window_seconds=60
        )

    assert error.value.status_code == 429
    assert error.value.code == "RATE_LIMIT_EXCEEDED"
    assert error.value.retry_after_seconds >= 1
