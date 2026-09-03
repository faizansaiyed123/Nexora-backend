from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup


class CollectionResult:
    """
    Result returned by the generic collection service.

    The service only performs HTTP collection and extraction.
    It does not know anything about a particular source, website,
    product, marketplace, or industry.
    """

    def __init__(
        self,
        price: Decimal | None,
        availability: str | None,
        attributes: dict[str, Any],
        response_time_ms: int,
        http_status_code: int | None,
        currency: str | None = None,
        success: bool = True,
        error: str | None = None,
        raw_payload: str | None = None,
        url: str | None = None,
    ):
        self.price = price
        self.currency = currency
        self.availability = availability
        self.attributes = attributes
        self.response_time_ms = response_time_ms
        self.http_status_code = http_status_code
        self.success = success
        self.error = error
        self.raw_payload = raw_payload
        self.url = url


class CollectionService:
    """
    Generic HTTP collection service.

    Responsibilities:
    - Fetch a dynamic target URL
    - Follow redirects
    - Measure request duration
    - Preserve the raw response
    - Extract structured information
    - Normalize common price/currency/availability values

    This service contains NO website-specific logic.
    """

    DEFAULT_TIMEOUT = 30.0

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,image/webp,"
            "*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def collect(
        self,
        target_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Optional[dict[str, str]] = None,
    ) -> CollectionResult:
        """
        Fetch and extract information from a dynamic URL.
        """

        request_headers = self.DEFAULT_HEADERS.copy()

        if headers:
            request_headers.update(headers)

        start_time = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=request_headers,
            ) as client:

                response = await client.get(target_url)

                response_time_ms = int(
                    (time.perf_counter() - start_time) * 1000
                )

                final_url = str(response.url)

                raw_payload = response.text

                # -------------------------------------------------
                # HTTP failure
                # -------------------------------------------------

                if response.status_code >= 400:
                    return CollectionResult(
                        price=None,
                        currency=None,
                        availability=None,
                        attributes={},
                        response_time_ms=response_time_ms,
                        http_status_code=response.status_code,
                        success=False,
                        error=f"HTTP {response.status_code}",
                        raw_payload=raw_payload,
                        url=final_url,
                    )

                # -------------------------------------------------
                # Determine response type
                # -------------------------------------------------

                content_type = (
                    response.headers.get(
                        "content-type",
                        "",
                    )
                    .lower()
                )

                # -------------------------------------------------
                # JSON response
                # -------------------------------------------------

                if (
                    "application/json" in content_type
                    or "application/ld+json" in content_type
                ):
                    extracted = self._extract_from_json(
                        raw_payload
                    )

                # -------------------------------------------------
                # HTML response
                # -------------------------------------------------

                else:
                    soup = BeautifulSoup(
                        raw_payload,
                        "html.parser",
                    )

                    extracted = self._extract(
                        soup=soup,
                        base_url=final_url,
                    )

                return CollectionResult(
                    price=extracted["price"],
                    currency=extracted["currency"],
                    availability=extracted["availability"],
                    attributes=extracted["attributes"],
                    response_time_ms=response_time_ms,
                    http_status_code=response.status_code,
                    success=True,
                    error=None,
                    raw_payload=raw_payload,
                    url=final_url,
                )

        except httpx.TimeoutException:
            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResult(
                price=None,
                currency=None,
                availability=None,
                attributes={},
                response_time_ms=response_time_ms,
                http_status_code=None,
                success=False,
                error="Request timeout",
                raw_payload=None,
                url=target_url,
            )

        except httpx.HTTPError as exc:
            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResult(
                price=None,
                currency=None,
                availability=None,
                attributes={},
                response_time_ms=response_time_ms,
                http_status_code=None,
                success=False,
                error=str(exc),
                raw_payload=None,
                url=target_url,
            )

        except Exception as exc:
            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResult(
                price=None,
                currency=None,
                availability=None,
                attributes={},
                response_time_ms=response_time_ms,
                http_status_code=None,
                success=False,
                error=str(exc),
                raw_payload=None,
                url=target_url,
            )

    # =============================================================
    # HTML EXTRACTION
    # =============================================================

    def _extract(
        self,
        soup: BeautifulSoup,
        base_url: str,
    ) -> dict[str, Any]:
        """
        Generic HTML extraction.

        Priority:

        1. JSON-LD structured data
        2. Schema.org HTML attributes
        3. OpenGraph/meta data
        4. Generic HTML attributes
        5. Text-based fallback
        """

        attributes: dict[str, Any] = {}

        # ---------------------------------------------------------
        # Basic page information
        # ---------------------------------------------------------

        title = soup.find("title")

        if title:
            attributes["page_title"] = title.get_text(
                " ",
                strip=True,
            )

        description = soup.find(
            "meta",
            attrs={
                "name": re.compile(
                    r"^description$",
                    re.I,
                )
            },
        )

        if description and description.get("content"):
            attributes["description"] = (
                description["content"].strip()
            )

        canonical = soup.find(
            "link",
            attrs={"rel": "canonical"},
        )

        if canonical and canonical.get("href"):
            attributes["canonical_url"] = urljoin(
                base_url,
                canonical["href"],
            )

        # ---------------------------------------------------------
        # JSON-LD
        # ---------------------------------------------------------

        json_ld_data = self._extract_json_ld(soup)

        if json_ld_data:
            attributes["structured_data"] = json_ld_data

        price = self._extract_price_from_json_ld(
            json_ld_data
        )

        currency = self._extract_currency_from_json_ld(
            json_ld_data
        )

        availability = (
            self._extract_availability_from_json_ld(
                json_ld_data
            )
        )

        # ---------------------------------------------------------
        # Schema.org HTML
        # ---------------------------------------------------------

        if price is None:
            price = self._extract_schema_price(soup)

        if currency is None:
            currency = self._extract_schema_currency(
                soup
            )

        if availability is None:
            availability = self._extract_schema_availability(
                soup
            )

        # ---------------------------------------------------------
        # Meta/OpenGraph
        # ---------------------------------------------------------

        if price is None:
            price = self._extract_meta_price(soup)

        if currency is None:
            currency = self._extract_meta_currency(
                soup
            )

        if availability is None:
            availability = self._extract_meta_availability(
                soup
            )

        # ---------------------------------------------------------
        # Generic HTML
        # ---------------------------------------------------------

        if price is None:
            price = self._extract_generic_price(soup)

        if currency is None:
            currency = self._extract_generic_currency(
                soup
            )

        if availability is None:
            availability = self._extract_generic_availability(
                soup
            )

        return {
            "price": price,
            "currency": currency,
            "availability": availability,
            "attributes": attributes,
        }

    # =============================================================
    # JSON-LD
    # =============================================================

    def _extract_json_ld(
        self,
        soup: BeautifulSoup,
    ) -> list[Any]:
        """
        Extract all valid JSON-LD blocks.

        This is intentionally generic and does not assume
        a specific schema type.
        """

        results: list[Any] = []

        scripts = soup.find_all(
            "script",
            attrs={
                "type": re.compile(
                    r"application/ld\+json",
                    re.I,
                )
            },
        )

        for script in scripts:
            if not script.string:
                continue

            raw = script.string.strip()

            if not raw:
                continue

            try:
                parsed = json.loads(raw)

                if isinstance(parsed, list):
                    results.extend(parsed)
                else:
                    results.append(parsed)

            except (json.JSONDecodeError, TypeError):
                continue

        return results

    def _extract_price_from_json_ld(
        self,
        data: list[Any],
    ) -> Decimal | None:

        for item in self._walk_json(data):

            if not isinstance(item, dict):
                continue

            # Direct price
            if "price" in item:
                price = self._parse_price(
                    item.get("price")
                )

                if price is not None:
                    return price

            # Offers
            offers = item.get("offers")

            if offers:
                if isinstance(offers, dict):
                    offers = [offers]

                if isinstance(offers, list):
                    for offer in offers:
                        if not isinstance(offer, dict):
                            continue

                        price = self._parse_price(
                            offer.get("price")
                        )

                        if price is not None:
                            return price

        return None

    def _extract_currency_from_json_ld(
        self,
        data: list[Any],
    ) -> str | None:

        for item in self._walk_json(data):

            if not isinstance(item, dict):
                continue

            currency = self._normalize_currency(
                item.get("priceCurrency")
            )

            if currency:
                return currency

            offers = item.get("offers")

            if isinstance(offers, dict):
                currency = self._normalize_currency(
                    offers.get("priceCurrency")
                )

                if currency:
                    return currency

            if isinstance(offers, list):
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    currency = self._normalize_currency(
                        offer.get("priceCurrency")
                    )

                    if currency:
                        return currency

        return None

    def _extract_availability_from_json_ld(
        self,
        data: list[Any],
    ) -> str | None:

        for item in self._walk_json(data):

            if not isinstance(item, dict):
                continue

            availability = self._normalize_availability(
                item.get("availability")
            )

            if availability:
                return availability

            offers = item.get("offers")

            if isinstance(offers, dict):
                availability = (
                    self._normalize_availability(
                        offers.get("availability")
                    )
                )

                if availability:
                    return availability

            if isinstance(offers, list):
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    availability = (
                        self._normalize_availability(
                            offer.get("availability")
                        )
                    )

                    if availability:
                        return availability

        return None

    def _walk_json(
        self,
        value: Any,
    ):
        """
        Recursively walk arbitrary JSON structures.
        """

        if isinstance(value, dict):
            yield value

            for child in value.values():
                yield from self._walk_json(child)

        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json(child)

    # =============================================================
    # SCHEMA.ORG HTML
    # =============================================================

    def _extract_schema_price(
        self,
        soup: BeautifulSoup,
    ) -> Decimal | None:

        selectors = [
            '[itemprop="price"]',
        ]

        for selector in selectors:
            for element in soup.select(selector):

                value = (
                    element.get("content")
                    or element.get("value")
                    or element.get_text(
                        " ",
                        strip=True,
                    )
                )

                price = self._parse_price(value)

                if price is not None:
                    return price

        return None

    def _extract_schema_currency(
        self,
        soup: BeautifulSoup,
    ) -> str | None:

        element = soup.select_one(
            '[itemprop="priceCurrency"]'
        )

        if not element:
            return None

        value = (
            element.get("content")
            or element.get_text(
                " ",
                strip=True,
            )
        )

        return self._normalize_currency(value)

    def _extract_schema_availability(
        self,
        soup: BeautifulSoup,
    ) -> str | None:

        element = soup.select_one(
            '[itemprop="availability"]'
        )

        if not element:
            return None

        value = (
            element.get("href")
            or element.get("content")
            or element.get_text(
                " ",
                strip=True,
            )
        )

        return self._normalize_availability(value)

    # =============================================================
    # META
    # =============================================================

    def _extract_meta_price(
        self,
        soup: BeautifulSoup,
    ) -> Decimal | None:

        selectors = [
            'meta[property="product:price:amount"]',
            'meta[property="og:price:amount"]',
            'meta[name="price"]',
            'meta[name="product:price"]',
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if not element:
                continue

            price = self._parse_price(
                element.get("content")
            )

            if price is not None:
                return price

        return None

    def _extract_meta_currency(
        self,
        soup: BeautifulSoup,
    ) -> str | None:

        selectors = [
            'meta[property="product:price:currency"]',
            'meta[property="og:price:currency"]',
            'meta[name="currency"]',
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if not element:
                continue

            currency = self._normalize_currency(
                element.get("content")
            )

            if currency:
                return currency

        return None

    def _extract_meta_availability(
        self,
        soup: BeautifulSoup,
    ) -> str | None:

        selectors = [
            'meta[property="product:availability"]',
            'meta[property="og:availability"]',
            'meta[name="availability"]',
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if not element:
                continue

            availability = (
                self._normalize_availability(
                    element.get("content")
                )
            )

            if availability:
                return availability

        return None

    # =============================================================
    # GENERIC HTML
    # =============================================================

    def _extract_generic_price(
        self,
        soup: BeautifulSoup,
    ) -> Decimal | None:

        selectors = [
            "[data-price]",
            "[data-product-price]",
            "[data-current-price]",
            "[data-sale-price]",
            '[class*="price"]',
            '[id*="price"]',
        ]

        for selector in selectors:

            for element in soup.select(selector):

                value = (
                    element.get("data-price")
                    or element.get(
                        "data-product-price"
                    )
                    or element.get(
                        "data-current-price"
                    )
                    or element.get(
                        "data-sale-price"
                    )
                    or element.get("content")
                    or element.get_text(
                        " ",
                        strip=True,
                    )
                )

                price = self._parse_price(value)

                if price is not None:
                    return price

        return None

    def _extract_generic_currency(
        self,
        soup: BeautifulSoup,
    ) -> str | None:

        # Look at common attributes first.
        selectors = [
            "[data-currency]",
            "[data-price-currency]",
        ]

        for selector in selectors:

            for element in soup.select(selector):

                value = (
                    element.get("data-currency")
                    or element.get(
                        "data-price-currency"
                    )
                )

                currency = self._normalize_currency(
                    value
                )

                if currency:
                    return currency

        # Then inspect page text for currency symbols.
        text = soup.get_text(
            " ",
            strip=True,
        )

        return self._currency_from_text(text)

    def _extract_generic_availability(
        self,
        soup: BeautifulSoup,
    ) -> str | None:

        selectors = [
            "[data-availability]",
            "[data-stock]",
            '[class*="availability"]',
            '[id*="availability"]',
            '[class*="stock"]',
            '[id*="stock"]',
        ]

        for selector in selectors:

            for element in soup.select(selector):

                value = (
                    element.get(
                        "data-availability"
                    )
                    or element.get("data-stock")
                    or element.get_text(
                        " ",
                        strip=True,
                    )
                )

                availability = (
                    self._normalize_availability(
                        value
                    )
                )

                if availability:
                    return availability

        return None

    # =============================================================
    # NORMALIZATION
    # =============================================================

    @staticmethod
    def _normalize_currency(
        value: Any,
    ) -> str | None:

        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        if re.fullmatch(
            r"[A-Za-z]{3}",
            text,
        ):
            return text.upper()

        currency_map = {
            "$": "USD",
            "US$": "USD",
            "USD": "USD",
            "€": "EUR",
            "EUR": "EUR",
            "£": "GBP",
            "GBP": "GBP",
            "₹": "INR",
            "INR": "INR",
            "¥": "JPY",
            "JPY": "JPY",
            "CNY": "CNY",
            "CAD": "CAD",
            "AUD": "AUD",
        }

        return currency_map.get(
            text.upper()
        )

    @staticmethod
    def _currency_from_text(
        text: str,
    ) -> str | None:

        currency_patterns = [
            (r"\bUSD\b", "USD"),
            (r"\bEUR\b", "EUR"),
            (r"\bGBP\b", "GBP"),
            (r"\bINR\b", "INR"),
            (r"\bJPY\b", "JPY"),
            (r"\bCNY\b", "CNY"),
            (r"\bCAD\b", "CAD"),
            (r"\bAUD\b", "AUD"),
            (r"\$", "USD"),
            ("€", "EUR"),
            ("£", "GBP"),
            ("₹", "INR"),
            ("¥", "JPY"),
        ]

        for pattern, currency in currency_patterns:

            if re.search(pattern, text):
                return currency

        return None

    @staticmethod
    def _normalize_availability(
        value: Any,
    ) -> str | None:

        if value is None:
            return None

        text = str(value).lower().strip()

        if not text:
            return None

        # Most specific states first.

        if (
            "discontinued" in text
            or "no longer available" in text
        ):
            return "DISCONTINUED"

        if (
            "backorder" in text
            or "back order" in text
            or "back-order" in text
        ):
            return "BACKORDER"

        if (
            "preorder" in text
            or "pre-order" in text
            or "pre order" in text
        ):
            return "PREORDER"

        if (
            "outofstock" in text
            or "out of stock" in text
            or "sold out" in text
            or "currently unavailable" in text
        ):
            return "OUT_OF_STOCK"

        if (
            "instock" in text
            or "in stock" in text
            or "available" in text
        ):
            return "IN_STOCK"

        return None

    # =============================================================
    # PRICE PARSING
    # =============================================================

    @staticmethod
    def _parse_price(
        value: Any,
    ) -> Decimal | None:

        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        # Remove currency symbols and text.
        text = re.sub(
            r"[^\d.,]",
            "",
            text,
        )

        if not text:
            return None

        # ---------------------------------------------------------
        # Both comma and period.
        #
        # 1,299.99 -> 1299.99
        # 1.299,99 -> 1299.99
        # ---------------------------------------------------------

        if "," in text and "." in text:

            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "")
                text = text.replace(",", ".")

            else:
                text = text.replace(",", "")

        # ---------------------------------------------------------
        # Only comma.
        # ---------------------------------------------------------

        elif "," in text:

            parts = text.split(",")

            if len(parts[-1]) == 2:
                text = (
                    "".join(parts[:-1])
                    + "."
                    + parts[-1]
                )
            else:
                text = text.replace(",", "")

        # ---------------------------------------------------------
        # Only period.
        # ---------------------------------------------------------

        try:
            price = Decimal(text)

        except InvalidOperation:
            return None

        if price < 0:
            return None

        return price
