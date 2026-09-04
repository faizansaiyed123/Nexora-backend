"""
Pydantic v2 schemas for Website Discovery and Automated Catalog Extraction.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.models.enums import CreatedViaEnum, JobStatusEnum, OfferingTypeEnum


class DiscoveredProduct(BaseModel):
    """Evidence-based product candidate produced during website analysis."""

    name: Optional[str] = None
    url: Optional[str] = None
    price: Optional[Decimal] = None
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    sku: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    availability: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)
    extraction_method: str = "STRUCTURED"
    evidence: List[str] = Field(default_factory=list, exclude=True)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: Optional[str]) -> Optional[str]:
        return value.strip().upper() if value else None


class GeminiDiscoveryResult(BaseModel):
    """Strict structured response expected from Gemini; never persisted directly."""

    page_type: Literal["PRODUCT", "PRODUCT_LISTING", "OTHER", "UNKNOWN"] = "UNKNOWN"
    product_urls: List[str] = Field(default_factory=list)
    products: List[DiscoveredProduct] = Field(default_factory=list)


class DiscoveryRunRequest(BaseModel):
    website_url: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Client website URL to crawl and discover offerings from (must be a valid public HTTP/HTTPS URL)",
        examples=["https://example.com/shop"],
    )
    max_pages: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of linked pages to crawl during discovery",
        examples=[20],
    )
    default_offering_type: OfferingTypeEnum = Field(
        default=OfferingTypeEnum.PRODUCT,
        description="Default vertical offering type if not inferred from page markup",
        examples=[OfferingTypeEnum.PRODUCT],
    )

    @field_validator("website_url")
    @classmethod
    def validate_website_url(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("Website URL cannot be empty.")
        return clean

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "website_url": "https://example.com/shop",
                    "max_pages": 20,
                    "default_offering_type": "PRODUCT"
                }
            ]
        }
    )


class DiscoveredItemSummary(BaseModel):
    id: Optional[uuid.UUID] = None
    name: str
    sku: Optional[str] = None
    price: Optional[Decimal] = None
    currency: str = "USD"
    url: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)
    action: str = Field(..., description="Action taken: 'CREATED', 'UPDATED', or 'SKIPPED'")

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "123e4567-e89b-12d3-a456-426614174000",
                    "name": "Wireless Noise Cancelling Headphones",
                    "sku": "WH-1000XM4",
                    "price": 348.00,
                    "currency": "USD",
                    "url": "https://example.com/products/headphones",
                    "category": "Electronics > Audio",
                    "image_url": "https://example.com/images/headphones.jpg",
                    "attributes": {
                        "color": "Black",
                        "battery_life_hours": 30
                    },
                    "action": "CREATED"
                }
            ]
        }
    )


class DiscoveryJobResponse(BaseModel):
    job_id: uuid.UUID
    client_id: uuid.UUID
    target_url: str
    status: JobStatusEnum
    total_pages_crawled: int = 0
    total_items_processed: int = 0
    successful_items: int = 0
    failed_items: int = 0
    error_message: Optional[str] = None
    created_offerings_count: int = 0
    updated_offerings_count: int = 0
    items: List[DiscoveredItemSummary] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "job_id": "123e4567-e89b-12d3-a456-426614174000",
                    "client_id": "289e4ec1-da8d-4324-8d52-b9a47a737815",
                    "target_url": "https://example.com/shop",
                    "status": "COMPLETED",
                    "total_pages_crawled": 5,
                    "total_items_processed": 10,
                    "successful_items": 10,
                    "failed_items": 0,
                    "error_message": None,
                    "created_offerings_count": 8,
                    "updated_offerings_count": 2,
                    "items": [
                        {
                            "name": "Wireless Noise Cancelling Headphones",
                            "sku": "WH-1000XM4",
                            "price": 348.00,
                            "currency": "USD",
                            "url": "https://example.com/products/headphones",
                            "category": "Electronics > Audio",
                            "image_url": "https://example.com/images/headphones.jpg",
                            "attributes": {"color": "Black"},
                            "action": "CREATED"
                        }
                    ],
                    "started_at": "2026-09-04T12:00:00Z",
                    "completed_at": "2026-09-04T12:00:15Z"
                }
            ]
        }
    )
