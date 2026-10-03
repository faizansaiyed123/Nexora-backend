"""
Pydantic schemas for Alert Rules and Trigger Notification Logs.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import AlertTypeEnum


class AlertRuleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    alert_type: AlertTypeEnum
    threshold_value: Optional[float] = Field(None, description="Percentage or fixed difference threshold")
    target_channels: Dict[str, Any] = Field(default_factory=dict, description="e.g. {email: [...], webhook: ...}")
    cooldown_minutes: int = Field(default=60, ge=1, le=10080)
    is_active: bool = True


class AlertRuleCreate(AlertRuleBase):
    offering_id: Optional[uuid.UUID] = None


class AlertRuleUpdate(BaseModel):
    offering_id: Optional[uuid.UUID] = None
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    alert_type: Optional[AlertTypeEnum] = None
    threshold_value: Optional[float] = None
    target_channels: Optional[Dict[str, Any]] = None
    cooldown_minutes: Optional[int] = Field(None, ge=1, le=10080)
    is_active: Optional[bool] = None


class AlertRuleRead(AlertRuleBase):
    id: uuid.UUID
    client_id: uuid.UUID
    offering_id: Optional[uuid.UUID] = None
    last_triggered_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertLogRead(BaseModel):
    id: uuid.UUID
    alert_rule_id: uuid.UUID
    offering_match_id: Optional[uuid.UUID] = None
    title: str
    message: str
    triggered_value: Optional[Decimal] = None
    payload_snapshot: Dict[str, Any]
    is_read: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
