"""Initialize SQLite database directly using SQLAlchemy create_all."""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from backend.db.base import Base
import backend.models  # noqa: F401  - registers all models
from backend.core.config import get_settings

settings = get_settings()


async def init_db():
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("Database initialized successfully.")


asyncio.run(init_db())