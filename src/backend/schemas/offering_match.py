"""
Pydantic schemas for Offering Matches.
"""

import uuid
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from backend.models.enums import MatchStatusEnum


class OfferingMatchCreate(BaseModel):
    offering_id: uuid.UUID
    source_id: uuid.UUID
    target_url: HttpUrl

    match_status: MatchStatusEnum = MatchStatusEnum.APPROVED

    confidence_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
    )

    extraction_selectors: Optional[Dict[str, Any]] = None

    is_active: bool = True


class OfferingMatchUpdate(BaseModel):
    target_url: Optional[HttpUrl] = None

    match_status: Optional[MatchStatusEnum] = None

    confidence_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )

    extraction_selectors: Optional[Dict[str, Any]] = None

    is_active: Optional[bool] = None


class OfferingMatchRead(BaseModel):
    id: uuid.UUID
    offering_id: uuid.UUID
    source_id: uuid.UUID
    target_url: str
    match_status: MatchStatusEnum
    confidence_score: float
    extraction_selectors: Optional[Dict[str, Any]]
    is_active: bool

    model_config = ConfigDict(from_attributes=True)
