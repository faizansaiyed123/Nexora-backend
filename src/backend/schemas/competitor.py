"""
Pydantic schemas for Competitors, Scraper Sources, and Offering Matches.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import (
    CircuitStateEnum,
    CollectionMethodEnum,
    CompetitorStatusEnum,
    HealthStatusEnum,
    MatchStatusEnum,
    SourceTypeEnum,
    VerificationStatusEnum,
)


class CompetitorBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain: str = Field(..., min_length=3, max_length=255)
    status: CompetitorStatusEnum = Field(default=CompetitorStatusEnum.ACTIVE)


class CompetitorCreate(CompetitorBase):
    pass


class CompetitorUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    domain: Optional[str] = Field(None, min_length=3, max_length=255)
    status: Optional[CompetitorStatusEnum] = None


class CompetitorRead(CompetitorBase):
    id: uuid.UUID
    client_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SourceConfigurationBase(BaseModel):
    rate_limit_rpm: int = Field(default=30, ge=1, le=600)
    request_delay_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent_strategy: str = Field(default="ROTATE_CHROME", max_length=50)
    requires_proxy: bool = False
    requires_javascript: bool = False
    custom_headers: Dict[str, Any] = Field(default_factory=dict)
    extraction_selectors: Dict[str, Any] = Field(default_factory=dict)


class SourceConfigurationCreate(SourceConfigurationBase):
    pass


class SourceConfigurationUpdate(BaseModel):
    rate_limit_rpm: Optional[int] = Field(None, ge=1, le=600)
    request_delay_seconds: Optional[float] = Field(None, ge=0.0, le=60.0)
    timeout_seconds: Optional[int] = Field(None, ge=5, le=120)
    max_retries: Optional[int] = Field(None, ge=0, le=10)
    user_agent_strategy: Optional[str] = Field(None, max_length=50)
    requires_proxy: Optional[bool] = None
    requires_javascript: Optional[bool] = None
    custom_headers: Optional[Dict[str, Any]] = None
    extraction_selectors: Optional[Dict[str, Any]] = None


class SourceConfigurationRead(SourceConfigurationBase):
    id: uuid.UUID
    source_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SourceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    base_url: str = Field(..., min_length=5)
    source_type: SourceTypeEnum = Field(default=SourceTypeEnum.OFFICIAL_STORE)
    collection_method: CollectionMethodEnum = Field(default=CollectionMethodEnum.HTTP_FAST)


class SourceCreate(SourceBase):
    competitor_id: uuid.UUID
    configuration: Optional[SourceConfigurationCreate] = None


class SourceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    base_url: Optional[str] = Field(None, min_length=5)
    source_type: Optional[SourceTypeEnum] = None
    collection_method: Optional[CollectionMethodEnum] = None
    verification_status: Optional[VerificationStatusEnum] = None
    is_active: Optional[bool] = None


class SourceRead(SourceBase):
    id: uuid.UUID
    competitor_id: uuid.UUID
    verification_status: VerificationStatusEnum
    circuit_state: CircuitStateEnum
    failure_count: int
    consecutive_successes: int
    circuit_opened_at: Optional[datetime] = None
    circuit_half_opened_at: Optional[datetime] = None
    health_status: HealthStatusEnum
    is_active: bool
    created_at: datetime
    updated_at: datetime
    configuration: Optional[SourceConfigurationRead] = None

    model_config = ConfigDict(from_attributes=True)


class OfferingMatchBase(BaseModel):
    target_url: str = Field(..., min_length=5)
    match_status: MatchStatusEnum = Field(default=MatchStatusEnum.APPROVED)
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    extraction_selectors: Optional[Dict[str, Any]] = None


class OfferingMatchCreate(OfferingMatchBase):
    offering_id: uuid.UUID
    source_id: uuid.UUID


class OfferingMatchUpdate(BaseModel):
    target_url: Optional[str] = Field(None, min_length=5)
    match_status: Optional[MatchStatusEnum] = None
    confidence_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    extraction_selectors: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class OfferingMatchRead(OfferingMatchBase):
    id: uuid.UUID
    offering_id: uuid.UUID
    source_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
