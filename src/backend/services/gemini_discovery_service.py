"""Gemini-backed page understanding for discovery, isolated from persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Iterable, Optional

import httpx

from backend.core.config import get_settings
from backend.schemas.discovery import GeminiDiscoveryResult

logger = logging.getLogger("nexora.gemini_discovery")


class GeminiDiscoveryService:
    """Optional, schema-constrained fallback for ambiguous public web pages."""

    _MAX_CONTENT_CHARS = 25_000
    _MAX_LINKS = 100

    @classmethod
    def _clean_page_content(cls, raw_html: str) -> str:
        """Strip non-semantic markup, scripts, SVGs, and styles for clean, fast LLM comprehension."""
        if not raw_html:
            return ""
        text = re.sub(r"<script.*?</script>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<svg.*?</svg>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<noscript.*?</noscript>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    async def analyze_page(
        cls,
        *,
        page_url: str,
        page_text: str,
        candidate_urls: Iterable[str],
    ) -> Optional[GeminiDiscoveryResult]:
        settings = get_settings()
        if not settings.gemini_discovery_enabled or not settings.gemini_api_key:
            return None

        links = list(dict.fromkeys(candidate_urls))[: cls._MAX_LINKS]
        cleaned_content = cls._clean_page_content(page_text)[: cls._MAX_CONTENT_CHARS]

        prompt = (
            "Analyze this public webpage for product discovery. Return only facts explicitly "
            "present in PAGE_CONTENT or CANDIDATE_URLS. Never infer or guess a value. "
            "Classify the page as PRODUCT, PRODUCT_LISTING, OTHER, or UNKNOWN. A category, "
            "search, home, or collection page is PRODUCT_LISTING, not a product. For listings, "
            "return product_urls only when they are among CANDIDATE_URLS. Return products only "
            "when name and at least one product-specific evidence signal (price, SKU, product URL, "
            "or explicit Product structured data) are present. Use null for unavailable fields.\n\n"
            f"PAGE_URL: {page_url}\n"
            f"CANDIDATE_URLS:\n{json.dumps(links)}\n\n"
            f"PAGE_CONTENT:\n{cleaned_content}"
        )
        schema = {
            "type": "object",
            "properties": {
                "page_type": {"type": "string", "enum": ["PRODUCT", "PRODUCT_LISTING", "OTHER", "UNKNOWN"]},
                "product_urls": {"type": "array", "items": {"type": "string"}},
                "products": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": ["string", "null"]}, "url": {"type": ["string", "null"]},
                            "price": {"type": ["number", "null"]}, "currency": {"type": ["string", "null"]},
                            "sku": {"type": ["string", "null"]}, "brand": {"type": ["string", "null"]},
                            "category": {"type": ["string", "null"]}, "image_url": {"type": ["string", "null"]},
                            "availability": {"type": ["string", "null"]},
                            "attributes": {"type": "object", "additionalProperties": True},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "url", "price", "currency", "sku", "brand", "category", "image_url", "availability", "attributes", "evidence"],
                    },
                },
            },
            "required": ["page_type", "product_urls", "products"],
        }
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseJsonSchema": schema},
        }
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        endpoint,
                        headers={"x-goog-api-key": settings.gemini_api_key},
                        json=payload,
                    )
                    if response.status_code in (429, 502, 503) and attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    response.raise_for_status()
                text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                return GeminiDiscoveryResult.model_validate_json(text)
            except httpx.HTTPStatusError as exc:
                if attempt == 2 or exc.response.status_code not in (429, 502, 503):
                    logger.warning("Gemini discovery fallback failed with HTTP status %s.", exc.response.status_code)
                    return None
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                if attempt == 2:
                    logger.warning("Gemini discovery fallback failed without a usable structured response: %s", exc)
                    return None
