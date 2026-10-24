"""Isolated database helpers for the browser journey; run as subprocesses."""
from __future__ import annotations
import asyncio, json, sys, uuid
from sqlalchemy import select
from backend.core.security import get_password_hash
from backend.db.session import AsyncSessionLocal
from backend.models.client import UserModel
from backend.models.enums import RoleEnum
from backend.models.notification import NotificationOutboxModel

async def create_roles(client_id: uuid.UUID, suffix: str) -> dict[str, str]:
    result = {}
    async with AsyncSessionLocal() as session:
        for role in (RoleEnum.ANALYST, RoleEnum.VIEWER):
            email = f"{role.value.lower()}-{suffix}@northstar.example.com"
            user = await session.scalar(select(UserModel).where(UserModel.email == email))
            if user is None:
                user = UserModel(client_id=client_id, email=email, full_name=f"Northstar {role.value.title()}",
                    hashed_password=get_password_hash("Nexora-Role!2026"), role=role,
                    is_active=True, email_verified=True)
                session.add(user)
                await session.flush()
            result[role.value.lower()] = email
        await session.commit()
    return result

async def statuses(client_id: uuid.UUID) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(NotificationOutboxModel.status).where(
            NotificationOutboxModel.client_id == client_id, NotificationOutboxModel.channel == "email"))
        return list(result.scalars().all())

def main():
    command = sys.argv[1]
    if command == "create-roles":
        value = asyncio.run(create_roles(uuid.UUID(sys.argv[2]), sys.argv[3]))
    elif command == "notification-statuses":
        value = asyncio.run(statuses(uuid.UUID(sys.argv[2])))
    elif command == "dispatch-notifications":
        from backend.workers.tasks import dispatch_pending_notifications
        value = dispatch_pending_notifications.run()
    else:
        raise SystemExit(f"Unknown support command: {command}")
    print(json.dumps(value))

if __name__ == "__main__":
    main()
