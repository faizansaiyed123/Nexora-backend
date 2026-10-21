"""Fix jobs table to remove unique constraint and make endpoint resilient."""
import asyncio
from sqlalchemy import text
from backend.db.session import get_db


async def main():
    async for session in get_db():
        try:
            await session.execute(text("DROP INDEX IF EXISTS uq_jobs_active_scheduled_match"))
            await session.commit()
            print("Dropped unique index")
        except Exception as e:
            print(f"Could not drop index: {e}")
        
        result = await session.execute(
            text(
                "DELETE FROM jobs WHERE status = 'RUNNING'"
            )
        )
        await session.commit()
        print(f"Deleted {result.rowcount} stale RUNNING jobs")


asyncio.run(main())