import pytest
import pytest_asyncio
from backend.db.session import engine


@pytest_asyncio.fixture(autouse=True)
async def reset_engine_pool():
    """Ensure engine pool connections from prior event loops do not leak across tests on Windows."""
    yield
    await engine.dispose()
