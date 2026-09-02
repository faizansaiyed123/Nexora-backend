"""
Pydantic schemas for Dynamic Field Definitions and Catalog Offerings.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import CreatedViaEnum, OfferingTypeEnum


class DynamicFieldDefinitionBase(BaseModel):
    field_name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    display_label: str = Field(..., min_length=1, max_length=255)
    data_type: str = Field(default="STRING", max_length=50)
    unit_of_measure: Optional[str] = Field(None, max_length=50)
    is_comparable: bool = True
    is_required: bool = False
    options: List[str] = Field(default_factory=list)


class DynamicFieldDefinitionCreate(DynamicFieldDefinitionBase):
    pass


class DynamicFieldDefinitionRead(DynamicFieldDefinitionBase):
    id: uuid.UUID
    client_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OfferingBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    sku: Optional[str] = Field(None, max_length=100)
    brand: Optional[str] = Field(None, max_length=255)
    category_path: Optional[str] = Field(None, max_length=500)
    offering_type: OfferingTypeEnum = Field(default=OfferingTypeEnum.PRODUCT)
    base_price: Optional[Decimal] = Field(None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    dynamic_attributes: Dict[str, Any] = Field(default_factory=dict)
    meta_info: Dict[str, Any] = Field(default_factory=dict)


class OfferingCreate(OfferingBase):
    created_via: CreatedViaEnum = Field(default=CreatedViaEnum.MANUAL)


class OfferingUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=500)
    sku: Optional[str] = Field(None, max_length=100)
    brand: Optional[str] = Field(None, max_length=255)
    category_path: Optional[str] = Field(None, max_length=500)
    offering_type: Optional[OfferingTypeEnum] = None
    base_price: Optional[Decimal] = Field(None, ge=0)
    currency: Optional[str] = Field(None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    is_active: Optional[bool] = None
    dynamic_attributes: Optional[Dict[str, Any]] = None
    meta_info: Optional[Dict[str, Any]] = None


class OfferingRead(OfferingBase):
    id: uuid.UUID
    client_id: uuid.UUID
    is_active: bool
    created_via: CreatedViaEnum
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OfferingFilterParams(BaseModel):
    query: Optional[str] = None
    brand: Optional[str] = None
    category_path: Optional[str] = None
    offering_type: Optional[OfferingTypeEnum] = None
    is_active: Optional[bool] = None
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
