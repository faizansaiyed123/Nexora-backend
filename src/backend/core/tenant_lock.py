"""PostgreSQL transaction-scoped advisory locks for tenant quotas."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _lock_key(namespace: str, client_id: UUID) -> str:
    return f"nexora:{namespace}:{client_id}"


async def acquire_tenant_lock(db: AsyncSession, client_id: UUID, namespace: str) -> None:
    """Serialize a tenant-local quota mutation for the duration of the transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": _lock_key(namespace, client_id)},
    )
