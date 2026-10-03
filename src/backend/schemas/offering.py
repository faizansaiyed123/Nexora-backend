"""
Pydantic v2 schemas for Offerings & Dynamic Catalog Management.
Includes single CRUD, pagination, bulk import (up to 1,000 items), bulk archive,
dynamic field definitions, auto-discovery, and competitor enrichment summary metrics.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from backend.models.enums import (
    AvailabilityStatusEnum,
    CreatedViaEnum,
    MatchStatusEnum,
    OfferingTypeEnum,
)


# ============================================================================
# Dynamic Field Definition Schemas
# ============================================================================

class DynamicFieldDefinitionBase(BaseModel):
    field_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Attribute machine key (alphanumeric, underscore, dash)",
        examples=["battery_life_hours"],
    )

    display_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="User-friendly attribute label",
        examples=["Battery Life (Hours)"],
    )

    data_type: str = Field(
        default="STRING",
        max_length=50,
        description="Data type (STRING, NUMBER, BOOLEAN, DECIMAL, DATE, JSON)",
        examples=["NUMBER"],
    )

    unit: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Unit of measurement (e.g. hours, kg, mm, USD)",
        examples=["hours"],
    )

    category: Optional[str] = Field(
        default="GENERAL",
        max_length=50,
        description="Attribute category grouping",
        examples=["Technical Specs"],
    )

    description: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Documentation or guidance for this dynamic field",
        examples=["Estimated operating time on full charge."],
    )

    is_selected: bool = Field(
        default=True,
        description="Whether this attribute is selected for display and monitoring",
        examples=[True],
    )

    confidence: str = Field(
        default="HIGH",
        max_length=20,
        description="Extraction confidence level (HIGH, MEDIUM, LOW)",
        examples=["HIGH"],
    )

    is_required: bool = Field(
        default=False,
        description="Whether this attribute is mandatory for new offerings",
        examples=[False],
    )

    is_comparable: bool = Field(
        default=True,
        description="Whether this attribute can be benchmarked against competitor offerings",
        examples=[True],
    )

    options: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Allowed options or validation constraints for enum/select types",
        examples=[{"choices": ["Red", "Blue", "Black"]}],
        json_schema_extra={"example": {"choices": ["Standard", "Deluxe", "Suite"]}},
    )

    @field_validator("data_type")
    @classmethod
    def normalize_data_type(cls, v: str) -> str:
        return v.strip().upper() if v else "STRING"


class DynamicFieldDefinitionCreate(DynamicFieldDefinitionBase):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "field_name": "battery_life_hours",
                    "display_name": "Battery Life (Hours)",
                    "data_type": "NUMBER",
                    "unit": "hours",
                    "category": "Technical Specs",
                    "description": "Rated playback hours on single charge.",
                    "is_selected": True,
                    "confidence": "HIGH",
                    "is_required": False,
                    "is_comparable": True
                }
            ]
        }
    )


class DynamicFieldDefinitionRead(DynamicFieldDefinitionBase):
    id: uuid.UUID
    client_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "123e4567-e89b-12d3-a456-426614174000",
                    "client_id": "289e4ec1-da8d-4324-8d52-b9a47a737815",
                    "field_name": "battery_life_hours",
                    "display_name": "Battery Life (Hours)",
                    "data_type": "NUMBER",
                    "unit": "hours",
                    "category": "Technical Specs",
                    "description": "Rated playback hours on single charge.",
                    "is_selected": True,
                    "confidence": "HIGH",
                    "is_required": False,
                    "is_comparable": True,
                    "created_at": "2026-09-01T12:00:00Z"
                }
            ]
        }
    )


# ============================================================================
# Competitor Match Enrichment Schemas
# ============================================================================

class CompetitorMatchSummary(BaseModel):
    match_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: str
    source_id: uuid.UUID
    source_name: str
    target_url: str
    match_status: MatchStatusEnum
    confidence_score: float
    current_price: Optional[Decimal] = None
    currency: Optional[str] = "USD"
    availability: Optional[AvailabilityStatusEnum] = None
    last_observed_at: Optional[datetime] = None
    price_delta_percent: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


# ============================================================================
# Core Offering Schemas
# ============================================================================

class OfferingBase(BaseModel):
    """
    Universal canonical offering schema supporting all business verticals.
    Fixed core fields + sanitized JSONB dynamic attributes.
    """
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Offering title / display name",
        examples=["Pro Gaming Wireless Headset X1"],
    )

    offering_type: OfferingTypeEnum = Field(
        default=OfferingTypeEnum.CUSTOM,
        description="Vertical offering classification (PRODUCT, SERVICE, SAAS_PLAN, HOTEL_ROOM, RENTAL, etc.)",
        examples=[OfferingTypeEnum.PRODUCT],
    )

    custom_type_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Custom vertical type label if offering_type is CUSTOM",
        examples=["Equipment Lease"],
    )

    sku: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Stock Keeping Unit or internal identifier code",
        examples=["HDST-X1-BLK"],
    )

    current_price: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Benchmark catalog price (Exact monetary Decimal)",
        examples=[Decimal("149.99")],
    )

    currency: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
        description="3-letter ISO 4217 currency code",
        examples=["USD"],
    )

    market: str = Field(
        default="US",
        min_length=2,
        max_length=2,
        pattern=r"^[A-Z]{2}$",
        description="2-letter ISO 3166-1 alpha-2 target market region code",
        examples=["US"],
    )

    url: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Official landing page / store product URL",
        examples=["https://example.com/products/headset-x1"],
    )

    category: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Product or service catalog hierarchy category",
        examples=["Electronics > Audio"],
    )

    image_url: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Primary display image URL",
        examples=["https://example.com/images/headset-x1.jpg"],
    )

    description: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="Full product/service marketing and specification description",
        examples=["Next-generation low-latency wireless gaming headset with hybrid ANC."],
    )

    attributes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Sanitized domain-specific dynamic attributes (max 50 keys, 2 levels deep)",
        examples=[{
            "brand": "AudioTech",
            "color": "Matte Black",
            "battery_life_hours": 30,
            "connectivity": "Wireless"
        }],
        json_schema_extra={"example": {
            "brand": "AudioTech",
            "color": "Matte Black",
            "battery_life_hours": 30,
            "connectivity": "Wireless"
        }},
    )

    is_monitored: bool = Field(
        default=True,
        description="Whether this offering is actively tracked for competitor intelligence",
        examples=[True],
    )

    created_via: CreatedViaEnum = Field(
        default=CreatedViaEnum.MANUAL,
        description="Ingestion origin channel",
        examples=[CreatedViaEnum.MANUAL],
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("Offering name cannot be empty.")
        return clean

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None

    @field_validator("market")
    @classmethod
    def normalize_market(cls, v: Optional[str]) -> str:
        return v.strip().upper() if v else "US"


class OfferingCreate(OfferingBase):
    """
    Schema for single or bulk offering creation.
    Includes backward-compatible alias mappings for base_price, source_url, dynamic_attributes.
    """
    # Optional legacy alias helpers that route to canonical fields
    base_price: Optional[Decimal] = None
    source_url: Optional[str] = None
    dynamic_attributes: Optional[Dict[str, Any]] = None
    identifiers: Optional[Dict[str, Any]] = None
    meta_info: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def remap_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Remap legacy names if canonical is not supplied
            if "base_price" in data and "current_price" not in data:
                data["current_price"] = data["base_price"]
            if "source_url" in data and "url" not in data:
                data["url"] = data["source_url"]
            if "dynamic_attributes" in data and "attributes" not in data:
                data["attributes"] = data["dynamic_attributes"]
            
            # If identifiers / meta_info provided, merge into attributes dict cleanly
            attrs = data.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            if "identifiers" in data and isinstance(data["identifiers"], dict):
                attrs["identifiers"] = data["identifiers"]
                if "sku" in data["identifiers"] and "sku" not in data:
                    data["sku"] = str(data["identifiers"]["sku"])
            if "meta_info" in data and isinstance(data["meta_info"], dict):
                attrs["meta_info"] = data["meta_info"]
            data["attributes"] = attrs
        return data

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "Pro Gaming Wireless Headset X1",
                    "offering_type": "PRODUCT",
                    "sku": "HDST-X1-BLK",
                    "current_price": 149.99,
                    "currency": "USD",
                    "market": "US",
                    "url": "https://example.com/products/headset-x1",
                    "category": "Electronics > Audio",
                    "image_url": "https://example.com/images/headset-x1.jpg",
                    "description": "Next-generation low-latency wireless gaming headset with ANC.",
                    "attributes": {
                        "brand": "AudioTech",
                        "color": "Matte Black",
                        "connectivity": "Wireless",
                        "battery_life_hours": 30
                    },
                    "is_monitored": True,
                    "created_via": "MANUAL"
                }
            ]
        }
    )


class OfferingUpdate(BaseModel):
    """
    Surgical partial update schema. Only explicitly provided fields are modified.
    """
    name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Updated offering title",
        examples=["Pro Gaming Wireless Headset X1 (2026 Edition)"],
    )

    offering_type: Optional[OfferingTypeEnum] = None
    custom_type_name: Optional[str] = None
    sku: Optional[str] = None

    current_price: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description="Updated price",
        examples=[Decimal("139.99")],
    )

    currency: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )

    market: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=2,
        pattern=r"^[A-Z]{2}$",
    )

    url: Optional[str] = Field(default=None, max_length=2000)
    category: Optional[str] = Field(default=None, max_length=100)
    image_url: Optional[str] = Field(default=None, max_length=2000)
    description: Optional[str] = Field(default=None, max_length=10000)

    attributes: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Partial attribute updates (merged with existing attributes)",
        examples=[{"color": "Midnight Black", "battery_life_hours": 35}],
        json_schema_extra={"example": {"color": "Midnight Black", "battery_life_hours": 35}},
    )

    is_monitored: Optional[bool] = None
    is_archived: Optional[bool] = None

    # Legacy aliases
    base_price: Optional[Decimal] = None
    source_url: Optional[str] = None
    dynamic_attributes: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def remap_legacy_update_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "base_price" in data and "current_price" not in data:
                data["current_price"] = data["base_price"]
            if "source_url" in data and "url" not in data:
                data["url"] = data["source_url"]
            if "dynamic_attributes" in data and "attributes" not in data:
                data["attributes"] = data["dynamic_attributes"]
            if "is_active" in data and "is_archived" not in data:
                data["is_archived"] = not data["is_active"]
        return data

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            clean = v.strip()
            if not clean:
                raise ValueError("Offering name cannot be empty.")
            return clean
        return None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None

    @field_validator("market")
    @classmethod
    def normalize_market(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "Pro Gaming Wireless Headset X1 (2026 Edition)",
                    "current_price": 139.99,
                    "attributes": {
                        "color": "Midnight Black",
                        "battery_life_hours": 35
                    },
                    "is_monitored": True
                }
            ]
        }
    )


class OfferingRead(OfferingBase):
    id: uuid.UUID
    client_id: uuid.UUID
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    # Competitor intelligence summary metrics
    competitor_matches_count: int = 0
    min_competitor_price: Optional[Decimal] = None
    avg_competitor_price: Optional[Decimal] = None
    price_delta_percent: Optional[Decimal] = None

    # Backward compatibility properties
    @computed_field
    @property
    def base_price(self) -> Optional[Decimal]:
        return self.current_price

    @computed_field
    @property
    def source_url(self) -> Optional[str]:
        return self.url

    @computed_field
    @property
    def dynamic_attributes(self) -> Dict[str, Any]:
        return self.attributes

    @computed_field
    @property
    def identifiers(self) -> Dict[str, Any]:
        if isinstance(self.attributes, dict) and "identifiers" in self.attributes:
            return self.attributes["identifiers"]
        if self.sku:
            return {"sku": self.sku}
        return {}

    @computed_field
    @property
    def meta_info(self) -> Dict[str, Any]:
        if isinstance(self.attributes, dict) and "meta_info" in self.attributes:
            return self.attributes["meta_info"]
        return {}

    @computed_field
    @property
    def is_active(self) -> bool:
        return not self.is_archived

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "123e4567-e89b-12d3-a456-426614174000",
                    "client_id": "289e4ec1-da8d-4324-8d52-b9a47a737815",
                    "name": "Pro Gaming Wireless Headset X1",
                    "offering_type": "PRODUCT",
                    "custom_type_name": None,
                    "sku": "HDST-X1-BLK",
                    "current_price": 149.99,
                    "currency": "USD",
                    "market": "US",
                    "url": "https://example.com/products/headset-x1",
                    "category": "Electronics > Audio",
                    "image_url": "https://example.com/images/headset-x1.jpg",
                    "description": "Next-generation low-latency wireless gaming headset with ANC.",
                    "attributes": {
                        "brand": "AudioTech",
                        "color": "Matte Black",
                        "connectivity": "Wireless",
                        "battery_life_hours": 30
                    },
                    "is_archived": False,
                    "is_monitored": True,
                    "created_via": "MANUAL",
                    "competitor_matches_count": 3,
                    "min_competitor_price": 139.99,
                    "avg_competitor_price": 145.50,
                    "price_delta_percent": -6.67,
                    "created_at": "2026-09-01T12:00:00Z",
                    "updated_at": "2026-09-03T18:30:00Z"
                }
            ]
        }
    )


class OfferingHistoryRead(BaseModel):
    offering_match_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: str
    source_id: uuid.UUID
    source_name: str
    target_url: str
    observed_price: Optional[Decimal] = None
    currency: str
    availability: AvailabilityStatusEnum
    response_time_ms: int
    http_status_code: int
    observed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OfferingDetailRead(OfferingRead):
    matches: List[CompetitorMatchSummary] = Field(
        default_factory=list,
        description="Detailed list of matched competitor offerings and live price snapshots"
    )


# ============================================================================
# Pagination & Bulk Ingestion Schemas
# ============================================================================

class OfferingPaginationResponse(BaseModel):
    items: List[OfferingRead]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_more: bool


class BulkOfferingImportRequest(BaseModel):
    items: List[OfferingCreate] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="List of offerings to import (maximum 1,000 per request)",
    )


class BulkImportErrorItem(BaseModel):
    index: int = Field(..., description="Zero-based index of the failed offering in the input array")
    sku: Optional[str] = None
    name: Optional[str] = None
    error: str = Field(..., description="Validation or business rule failure reason")


class BulkOfferingImportResponse(BaseModel):
    total_processed: int
    successful_count: int
    failed_count: int
    errors: List[BulkImportErrorItem] = Field(default_factory=list)


class BulkOfferingArchiveRequest(BaseModel):
    offering_ids: List[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="List of offering UUIDs to soft-archive (maximum 1,000)",
    )


class BulkOfferingArchiveResponse(BaseModel):
    total_requested: int
    archived_count: int
    archived_ids: List[uuid.UUID]


class ToggleMonitoringResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_monitored: bool
    message: str
