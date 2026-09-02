"""
Pydantic schemas for Enterprise Tenant Audit & Compliance Trail.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    changes: Dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
