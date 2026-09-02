"""
Enterprise Security Audit Logging Service.
"""

import logging
import uuid
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from backend.models.audit import AuditLogModel

logger = logging.getLogger(__name__)


class AuditService:
    @classmethod
    async def log_security_event(
        cls,
        session: AsyncSession,
        action: str,
        client_id: uuid.UUID,
        user_id: Optional[uuid.UUID] = None,
        resource_type: str = "AUTH",
        resource_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        changes: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            safe_changes = {}
            if changes:
                for k, v in changes.items():
                    if any(secret_term in k.lower() for secret_term in ["password", "token", "secret", "hash"]):
                        safe_changes[k] = "[REDACTED]"
                    else:
                        safe_changes[k] = v

            entry = AuditLogModel(
                client_id=client_id,
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id or (str(user_id) if user_id else None),
                ip_address=ip_address,
                user_agent=user_agent,
                changes=safe_changes,
            )
            session.add(entry)
        except Exception as e:
            logger.error(f"Failed to record audit log: {e}")
