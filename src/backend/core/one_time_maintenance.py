"""Guarded one-time maintenance operation for the Nexora Postgres database.

This code does nothing unless two explicit environment switches are present.
It is temporary and will be removed immediately after the requested reset.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import text

from backend.db.session import engine

logger = logging.getLogger(__name__)

_TARGET_DATABASE = "nexora_db_b73y"
_REQUIRED_OPERATION_VALUE = "PURGE_NEXORA_PUBLIC_DATA"
_REQUIRED_DATABASE_CONFIRMATION = "nexora_db_b73y"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def purge_public_application_data_once() -> None:
    """Clear rows from user/application tables while preserving schema migrations."""
    if os.getenv("NEXORA_PURGE_PUBLIC_DATA") != _REQUIRED_OPERATION_VALUE:
        return

    # Require a second explicit confirmation and hard-code the expected logical
    # DB name so an accidental environment-variable copy cannot wipe another DB.
    if os.getenv("NEXORA_PURGE_EXPECTED_DATABASE") != _REQUIRED_DATABASE_CONFIRMATION:
        raise RuntimeError("Database purge refused: expected database confirmation is missing.")

    deleted_rows = 0
    table_count = 0

    async with engine.begin() as connection:
        database_name = (await connection.execute(text("SELECT current_database()"))).scalar_one()
        if database_name != _TARGET_DATABASE:
            raise RuntimeError("Database purge refused: connected database does not match Nexora target.")

        tables_result = await connection.execute(
            text(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ('information_schema', 'pg_catalog')
                  AND table_schema NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND NOT (table_schema = 'public' AND table_name = 'alembic_version')
                ORDER BY table_schema, table_name
                """
            )
        )
        tables = [(row[0], row[1]) for row in tables_result.fetchall()]
        if not tables:
            logger.warning("NEXORA_DB_PURGE_NO_TABLES database=%s", database_name)
            return

        quoted_tables = [
            f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
            for schema, table in tables
        ]

        for schema, table in tables:
            result = await connection.execute(
                text(f"SELECT count(*) FROM {_quote_identifier(schema)}.{_quote_identifier(table)}")
            )
            deleted_rows += int(result.scalar_one())

        # One atomic TRUNCATE preserves all table definitions and the Alembic
        # migration marker while clearing data and resetting owned sequences.
        statement = "TRUNCATE TABLE " + ", ".join(quoted_tables) + " RESTART IDENTITY CASCADE"
        await connection.exec_driver_sql(statement)
        table_count = len(tables)

        remaining_rows = 0
        for schema, table in tables:
            result = await connection.execute(
                text(f"SELECT count(*) FROM {_quote_identifier(schema)}.{_quote_identifier(table)}")
            )
            remaining_rows += int(result.scalar_one())

        if remaining_rows != 0:
            raise RuntimeError(
                f"Database purge verification failed: {remaining_rows} application rows remain."
            )

    # Log only aggregate counts, never customer data or email addresses.
    logger.warning(
        "NEXORA_DB_PURGE_COMPLETED database=%s tables_cleared=%d rows_cleared=%d migration_history=preserved",
        _TARGET_DATABASE,
        table_count,
        deleted_rows,
    )
