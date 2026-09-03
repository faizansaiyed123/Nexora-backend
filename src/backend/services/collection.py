from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urljoin

import re

import httpx
from bs4 import BeautifulSoup


@dataclass
class CollectionResult:
    success: bool
    url: str
    status_code: Optional[int]
    response_time_ms: int
    price: Optional[Decimal]
    currency: Optional[str]
    availability: Optional[str]
    attributes: dict[str, Any]
    error: Optional[str] = None


class CollectionService:
    """
    Generic HTTP collection service.

    This is intentionally industry-independent.
    It does not assume Amazon, products, or any particular website structure.
    """

    DEFAULT_TIMEOUT = 30.0

    async def collect(
        self,
        url: str,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Optional[dict[str, str]] = None,
    ) -> CollectionResult:

        request_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        if headers:
            request_headers.update(headers)

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    url,
                    headers=request_headers,
                )

                html = response.text

                if response.status_code >= 400:
                    return CollectionResult(
                        success=False,
                        url=str(response.url),
                        status_code=response.status_code,
                        response_time_ms=0,
                        price=None,
                        currency=None,
                        availability=None,
                        attributes={},
                        error=f"HTTP {response.status_code}",
                    )

                extracted = self._extract(html, str(response.url))

                return CollectionResult(
                    success=True,
                    url=str(response.url),
                    status_code=response.status_code,
                    response_time_ms=0,
                    price=extracted["price"],
                    currency=extracted["currency"],
                    availability=extracted["availability"],
                    attributes=extracted["attributes"],
                )

        except httpx.TimeoutException:
            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=0,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                error="Request timeout",
            )

        except httpx.HTTPError as exc:
            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=0,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                error=str(exc),
            )

        except Exception as exc:
            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=0,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                error=str(exc),
            )

    def _extract(self, html: str, base_url: str) -> dict[str, Any]:
        """
        Generic extraction.

        Priority:
        1. JSON-LD structured data
        2. Meta tags
        3. Generic HTML text candidates
        """

        soup = BeautifulSoup(html, "html.parser")

        price = self._extract_price(soup)
        currency = self._extract_currency(soup)
        availability = self._extract_availability(soup)

        attributes: dict[str, Any] = {}

        title = soup.find("title")
        if title:
            attributes["page_title"] = title.get_text(" ", strip=True)

        description = soup.find(
            "meta",
            attrs={"name": re.compile("^description$", re.I)},
        )
        if description and description.get("content"):
            attributes["description"] = description["content"]

        canonical = soup.find(
            "link",
            attrs={"rel": "canonical"},
        )
        if canonical and canonical.get("href"):
            attributes["canonical_url"] = urljoin(
                base_url,
                canonical["href"],
            )

        return {
            "price": price,
            "currency": currency,
            "availability": availability,
            "attributes": attributes,
        }

    def _extract_price(self, soup: BeautifulSoup) -> Optional[Decimal]:
        """
        Generic price detection.

        This is deliberately conservative.
        It does not blindly treat every number on a page as a price.
        """

        selectors = [
            '[itemprop="price"]',
            '[data-price]',
            '[class*="price"]',
            '[id*="price"]',
        ]

        for selector in selectors:
            for element in soup.select(selector):

                value = (
                    element.get("content")
                    or element.get("data-price")
                    or element.get_text(" ", strip=True)
                )

                price = self._parse_price(value)

                if price is not None:
                    return price

        return None

    def _extract_currency(self, soup: BeautifulSoup) -> Optional[str]:

        currency_element = soup.select_one(
            '[itemprop="priceCurrency"]'
        )

        if currency_element:
            value = (
                currency_element.get("content")
                or currency_element.get_text(strip=True)
            )

            if value and re.fullmatch(r"[A-Za-z]{3}", value):
                return value.upper()

        meta = soup.find(
            "meta",
            attrs={"property": "product:price:currency"},
        )

        if meta and meta.get("content"):
            value = meta["content"].strip()

            if re.fullmatch(r"[A-Za-z]{3}", value):
                return value.upper()

        text = soup.get_text(" ", strip=True)

        currency_patterns = {
            "$": "USD",
            "€": "EUR",
            "£": "GBP",
            "₹": "INR",
            "¥": "JPY",
        }

        for symbol, currency in currency_patterns.items():
            if symbol in text:
                return currency

        return None

    def _extract_availability(
        self,
        soup: BeautifulSoup,
    ) -> Optional[str]:

        element = soup.select_one(
            '[itemprop="availability"]'
        )

        if element:
            value = (
                element.get("href")
                or element.get("content")
                or element.get_text(" ", strip=True)
            )

            if value:
                value = value.lower()

                if "instock" in value or "in stock" in value:
                    return "IN_STOCK"

                if "outofstock" in value or "out of stock" in value:
                    return "OUT_OF_STOCK"

                if "preorder" in value:
                    return "PREORDER"

        return None

    @staticmethod
    def _parse_price(value: Any) -> Optional[Decimal]:

        if value is None:
            return None

        text = str(value).strip()

        # Remove currency symbols and other non-numeric characters.
        text = re.sub(r"[^\d.,]", "", text)

        if not text:
            return None

        # Handle common formats:
        # 399.99
        # 399,99
        # 1,299.99
        # 1.299,99

        if "," in text and "." in text:

            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "")
                text = text.replace(",", ".")
            else:
                text = text.replace(",", "")

        elif "," in text:
            parts = text.split(",")

            if len(parts[-1]) == 2:
                text = "".join(parts[:-1]) + "." + parts[-1]
            else:
                text = text.replace(",", "")

        try:
            price = Decimal(text)

            if price < 0:
                return None

            return price

        except InvalidOperation:
            return None
