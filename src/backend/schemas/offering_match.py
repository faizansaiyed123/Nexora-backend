"""
Pydantic schemas for Offering Matches.
"""

import uuid
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from backend.models.enums import MatchStatusEnum


class OfferingMatchCreate(BaseModel):
    offering_id: uuid.UUID = Field(
        ...,
        description="Target client offering UUID",
        examples=["123e4567-e89b-12d3-a456-426614174000"],
    )
    source_id: uuid.UUID = Field(
        ...,
        description="Competitor scraper source UUID",
        examples=["234e5678-e89b-12d3-a456-426614174111"],
    )
    target_url: HttpUrl = Field(
        ...,
        description="Competitor product / listing page URL",
        examples=["https://competitor.com/products/wh-1000xm5-silver"],
    )

    match_status: MatchStatusEnum = Field(
        default=MatchStatusEnum.APPROVED,
        description="Match verification status",
        examples=[MatchStatusEnum.APPROVED],
    )

    confidence_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="AI or algorithm match confidence score (0.0 to 1.0)",
        examples=[0.98],
    )

    extraction_selectors: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Custom CSS/XPath selector overrides for this specific competitor offering URL",
        examples=[{
            "price_selector": "span.price-current",
            "title_selector": "h1.product-title",
            "stock_selector": "div.stock-indicator"
        }],
        json_schema_extra={"example": {
            "price_selector": "span.price-current",
            "title_selector": "h1.product-title",
            "stock_selector": "div.stock-indicator"
        }},
    )

    is_active: bool = Field(
        default=True,
        description="Whether this match is actively monitored for price changes",
        examples=[True],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "offering_id": "123e4567-e89b-12d3-a456-426614174000",
                    "source_id": "234e5678-e89b-12d3-a456-426614174111",
                    "target_url": "https://competitor.com/products/wh-1000xm5-silver",
                    "match_status": "APPROVED",
                    "confidence_score": 0.98,
                    "extraction_selectors": {
                        "price_selector": "span.price-current",
                        "title_selector": "h1.product-title",
                        "stock_selector": "div.stock-indicator"
                    },
                    "is_active": True
                }
            ]
        }
    )


class OfferingMatchUpdate(BaseModel):
    target_url: Optional[HttpUrl] = Field(
        default=None,
        description="Updated competitor product URL",
        examples=["https://competitor.com/products/wh-1000xm5-silver-v2"],
    )

    match_status: Optional[MatchStatusEnum] = Field(
        default=None,
        description="Updated match verification status",
        examples=[MatchStatusEnum.APPROVED],
    )

    confidence_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Updated match confidence score (0.0 to 1.0)",
        examples=[1.0],
    )

    extraction_selectors: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Custom CSS/XPath selector overrides",
        examples=[{
            "price_selector": "span.price-current",
            "title_selector": "h1.product-title"
        }],
        json_schema_extra={"example": {
            "price_selector": "span.price-current",
            "title_selector": "h1.product-title"
        }},
    )

    is_active: Optional[bool] = Field(
        default=None,
        description="Toggle active monitoring",
        examples=[True],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "target_url": "https://competitor.com/products/wh-1000xm5-silver-v2",
                    "match_status": "APPROVED",
                    "confidence_score": 1.0,
                    "extraction_selectors": {
                        "price_selector": "span.price-current",
                        "title_selector": "h1.product-title"
                    },
                    "is_active": True
                }
            ]
        }
    )


class OfferingMatchRead(BaseModel):
    id: uuid.UUID
    offering_id: uuid.UUID
    source_id: uuid.UUID
    target_url: str
    match_status: MatchStatusEnum
    confidence_score: float
    extraction_selectors: Optional[Dict[str, Any]] = Field(
        default=None,
        examples=[{
            "price_selector": "span.price-current",
            "title_selector": "h1.product-title"
        }],
        json_schema_extra={"example": {
            "price_selector": "span.price-current",
            "title_selector": "h1.product-title"
        }},
    )
    is_active: bool

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "345e6789-e89b-12d3-a456-426614174222",
                    "offering_id": "123e4567-e89b-12d3-a456-426614174000",
                    "source_id": "234e5678-e89b-12d3-a456-426614174111",
                    "target_url": "https://competitor.com/products/wh-1000xm5-silver",
                    "match_status": "APPROVED",
                    "confidence_score": 0.98,
                    "extraction_selectors": {
                        "price_selector": "span.price-current",
                        "title_selector": "h1.product-title",
                        "stock_selector": "div.stock-indicator"
                    },
                    "is_active": True
                }
            ]
        }
    )
