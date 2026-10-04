import pytest
import pytest_asyncio
from backend.db.session import engine


@pytest_asyncio.fixture(autouse=True)
async def reset_engine_pool():
    """Ensure engine pool connections from prior event loops do not leak across tests on Windows."""
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def reset_redis_client():
    """Drop the cached Redis client between tests.

    rate_limit keeps a single module-level client. Its pooled sockets are bound
    to whichever event loop first opened them, so once pytest closes that loop
    every later Redis call raises "Event loop is closed" and silently degrades
    to the process-local fallback, which breaks atomicity assertions.
    """
    yield
    from backend.core import rate_limit

    client = rate_limit._redis_client
    rate_limit._redis_client = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass
