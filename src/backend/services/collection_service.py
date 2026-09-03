from decimal import Decimal
from typing import Any, Dict

import httpx
from bs4 import BeautifulSoup


class CollectionResult:
    def __init__(
        self,
        price: Decimal | None,
        availability: str,
        attributes: Dict[str, Any],
        response_time_ms: int,
        http_status_code: int,
        raw_payload: str | None = None,
    ):
        self.price = price
        self.availability = availability
        self.attributes = attributes
        self.response_time_ms = response_time_ms
        self.http_status_code = http_status_code
        self.raw_payload = raw_payload


class CollectionService:
    async def collect(self, target_url: str) -> CollectionResult:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            response = await client.get(target_url)

        response_time_ms = int(response.elapsed.total_seconds() * 1000)

        response.raise_for_status()

        html = response.text

        soup = BeautifulSoup(html, "html.parser")

        price = self._extract_price(soup)
        availability = self._extract_availability(soup)

        return CollectionResult(
            price=price,
            availability=availability,
            attributes={},
            response_time_ms=response_time_ms,
            http_status_code=response.status_code,
            raw_payload=html,
        )

    def _extract_price(self, soup: BeautifulSoup) -> Decimal | None:
        selectors = [
            '[itemprop="price"]',
            '[data-price]',
            ".price",
            "#price",
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if not element:
                continue

            value = (
                element.get("content")
                or element.get("data-price")
                or element.get_text(strip=True)
            )

            if not value:
                continue

            cleaned = (
                value.replace("$", "")
                .replace(",", "")
                .strip()
            )

            try:
                return Decimal(cleaned)
            except Exception:
                continue

        return None

    def _extract_availability(self, soup: BeautifulSoup) -> str:
        text = soup.get_text(" ", strip=True).lower()

        if "out of stock" in text:
            return "OUT_OF_STOCK"

        if "pre-order" in text or "preorder" in text:
            return "PREORDER"

        if "back order" in text or "backorder" in text:
            return "BACKORDER"

        if "in stock" in text:
            return "IN_STOCK"

        return "UNKNOWN"
