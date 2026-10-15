"""Tenant-scoped advisory locks for quota mutations.

Uses PostgreSQL transaction-scoped advisory locks when available and falls back
to a process-local threading lock for SQLite / non-PostgreSQL engines. The lock
is scoped to the (client_id, namespace) pair so concurrent requests for the same
tenant are serialized.
"""

import threading
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _lock_key(namespace: str, client_id: UUID) -> str:
    return f"nexora:{namespace}:{client_id}"


_local_locks: dict[str, threading.Lock] = {}
_local_locks_guard = threading.Lock()


def _acquire_local_lock(key: str) -> None:
    with _local_locks_guard:
        lock = _local_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _local_locks[key] = lock
    lock.acquire()


async def acquire_tenant_lock(db: AsyncSession, client_id: UUID, namespace: str) -> None:
    """Serialize a tenant-local quota mutation for the duration of the transaction."""
    key = _lock_key(namespace, client_id)
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        await db.flush()
    except Exception:
        # Fallback for SQLite / non-PostgreSQL engines.
        _acquire_local_lock(key)