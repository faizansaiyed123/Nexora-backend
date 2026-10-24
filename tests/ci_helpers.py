"""Disposable CI-only helpers for stateful browser journeys."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import select

from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel


async def seed_role(client_id: uuid.UUID, role: RoleEnum, email: str) -> None:
    async with AsyncSessionLocal() as session:
        existing = await session.scalar(select(UserModel).where(UserModel.email == email))
        if existing:
            return
        session.add(
            UserModel(
                client_id=client_id,
                email=email,
                full_name=f"Northstar {role.value.title()}",
                hashed_password=get_password_hash("Nexora-Role!2026"),
                role=role,
                is_active=True,
                email_verified=True,
            )
        )
        await session.commit()


async def notification_statuses(client_id: uuid.UUID) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NotificationOutboxModel.status).where(
                NotificationOutboxModel.client_id == client_id,
                NotificationOutboxModel.channel == "email",
            )
        )
        return list(result.scalars().all())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed-role", "dispatch-notifications", "notification-statuses"])
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()

    if args.action == "seed-role":
        if len(args.args) != 3:
            raise SystemExit("seed-role requires client UUID, role name, email")
        client_id = uuid.UUID(args.args[0])
        role = RoleEnum(args.args[1])
        asyncio.run(seed_role(client_id, role, args.args[2]))
        print(json.dumps({"seeded": args.args[2], "role": role.value}))
        return

    if args.action == "dispatch-notifications":
        from backend.workers.tasks import dispatch_pending_notifications
        print(json.dumps(dispatch_pending_notifications.run()))
        return

    if len(args.args) != 1:
        raise SystemExit("notification-statuses requires a client UUID")
    print(json.dumps(asyncio.run(notification_statuses(uuid.UUID(args.args[0])))))


