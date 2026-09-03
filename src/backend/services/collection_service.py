from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup


@dataclass
class CollectionResult:
    """
    Result returned by the generic collection service.

    HTTP success and extraction success are intentionally separate.
    A URL can return HTTP 200/202 while still producing no useful data.
    """

    success: bool
    url: str
    status_code: Optional[int]
    response_time_ms: int

    price: Optional[Decimal]
    currency: Optional[str]
    availability: Optional[str]

    attributes: dict[str, Any]

    raw_payload: Optional[str] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None

    extraction_status: str = "NO_DATA"
    error: Optional[str] = None

    @property
    def http_status_code(self) -> Optional[int]:
        return self.status_code


class CollectionService:
    """
    Generic, industry-independent HTTP collection service.

    Pipeline:

        URL
          ↓
        HTTP request
          ↓
        response inspection
          ↓
        JSON / JSON-LD
          ↓
        meta / OpenGraph
          ↓
        embedded structured data
          ↓
        generic HTML selectors
          ↓
        generic text detection
          ↓
        CollectionResult

    No website-specific selectors or hardcoded website names are used.
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
            "application/xml;q=0.9,"
            "application/json;q=0.8,"
            "*/*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    async def collect(
        self,
        url: str,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Optional[dict[str, str]] = None,
    ) -> CollectionResult:
        """
        Fetch a URL and perform generic extraction.
        """

        request_headers = self.DEFAULT_HEADERS.copy()

        if headers:
            request_headers.update(headers)

        start_time = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    url,
                    headers=request_headers,
                )

            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            final_url = str(response.url)
            content_type = response.headers.get(
                "content-type"
            )

            raw_payload = response.text

            content_length = len(
                response.content
            )

            # -------------------------------------------------
            # HTTP error
            # -------------------------------------------------

            if response.status_code >= 400:
                return CollectionResult(
                    success=False,
                    url=final_url,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    price=None,
                    currency=None,
                    availability=None,
                    attributes={
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "content_length": content_length,
                    },
                    raw_payload=raw_payload,
                    content_type=content_type,
                    content_length=content_length,
                    extraction_status="HTTP_ERROR",
                    error=f"HTTP {response.status_code}",
                )

            # -------------------------------------------------
            # JSON response
            # -------------------------------------------------

            if self._is_json_content(content_type, raw_payload):

                extracted = self._extract_json(
                    raw_payload
                )

                extraction_status = self._determine_extraction_status(
                    extracted
                )

                return CollectionResult(
                    success=True,
                    url=final_url,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    price=extracted["price"],
                    currency=extracted["currency"],
                    availability=extracted["availability"],
                    attributes={
                        **extracted["attributes"],
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "content_length": content_length,
                    },
                    raw_payload=raw_payload,
                    content_type=content_type,
                    content_length=content_length,
                    extraction_status=extraction_status,
                )

            # -------------------------------------------------
            # HTML response
            # -------------------------------------------------

            soup = BeautifulSoup(
                raw_payload,
                "html.parser",
            )

            extracted = self._extract_html(
                soup=soup,
                html=raw_payload,
                base_url=final_url,
            )

            extraction_status = self._determine_extraction_status(
                extracted
            )

            attributes = {
                **extracted["attributes"],
                "http_status": response.status_code,
                "content_type": content_type,
                "content_length": content_length,
            }

            # Useful diagnostic information.
            title = soup.find("title")

            if title:
                attributes["page_title"] = title.get_text(
                    " ",
                    strip=True,
                )

            # -------------------------------------------------
            # HTTP 202 diagnostics
            # -------------------------------------------------

            if response.status_code == 202:
                attributes["http_202"] = True

                if not raw_payload.strip():
                    attributes["empty_response"] = True

                if self._looks_like_dynamic_page(
                    soup,
                    raw_payload,
                ):
                    attributes["possible_dynamic_rendering"] = True

                if self._looks_like_challenge(
                    soup,
                    raw_payload,
                ):
                    attributes["possible_challenge"] = True

            return CollectionResult(
                success=True,
                url=final_url,
                status_code=response.status_code,
                response_time_ms=response_time_ms,
                price=extracted["price"],
                currency=extracted["currency"],
                availability=extracted["availability"],
                attributes=attributes,
                raw_payload=raw_payload,
                content_type=content_type,
                content_length=content_length,
                extraction_status=extraction_status,
            )

        except httpx.TimeoutException:

            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=response_time_ms,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                raw_payload=None,
                content_type=None,
                content_length=None,
                extraction_status="TIMEOUT",
                error="Request timeout",
            )

        except httpx.HTTPError as exc:

            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=response_time_ms,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                raw_payload=None,
                content_type=None,
                content_length=None,
                extraction_status="HTTP_ERROR",
                error=str(exc),
            )

        except Exception as exc:

            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=response_time_ms,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                raw_payload=None,
                content_type=None,
                content_length=None,
                extraction_status="ERROR",
                error=str(exc),
            )

    # =========================================================
    # HTML EXTRACTION
    # =========================================================

    def _extract_html(
        self,
        soup: BeautifulSoup,
        html: str,
        base_url: str,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        attributes: dict[str, Any] = {}

        # -----------------------------------------------------
        # 1. JSON-LD
        # -----------------------------------------------------

        jsonld_data = self._extract_jsonld(
            soup
        )

        if jsonld_data:

            jsonld_result = self._extract_from_structured_data(
                jsonld_data
            )

            price = jsonld_result["price"]
            currency = jsonld_result["currency"]
            availability = jsonld_result["availability"]

            attributes.update(
                jsonld_result["attributes"]
            )

        # -----------------------------------------------------
        # 2. Meta / OpenGraph
        # -----------------------------------------------------

        meta_result = self._extract_from_meta(
            soup
        )

        if price is None:
            price = meta_result["price"]

        if currency is None:
            currency = meta_result["currency"]

        if availability is None:
            availability = meta_result["availability"]

        attributes.update(
            meta_result["attributes"]
        )

        # -----------------------------------------------------
        # 3. Embedded JSON
        # -----------------------------------------------------

        embedded_result = self._extract_embedded_json(
            soup
        )

        if price is None:
            price = embedded_result["price"]

        if currency is None:
            currency = embedded_result["currency"]

        if availability is None:
            availability = embedded_result["availability"]

        attributes.update(
            embedded_result["attributes"]
        )

        # -----------------------------------------------------
        # 4. Generic HTML selectors
        # -----------------------------------------------------

        html_result = self._extract_from_html_selectors(
            soup
        )

        if price is None:
            price = html_result["price"]

        if currency is None:
            currency = html_result["currency"]

        if availability is None:
            availability = html_result["availability"]

        attributes.update(
            html_result["attributes"]
        )

        # -----------------------------------------------------
        # 5. Generic page text
        # -----------------------------------------------------

        text = soup.get_text(
            " ",
            strip=True,
        )

        if currency is None:
            currency = self._detect_currency_from_text(
                text
            )

        if availability is None:
            availability = self._detect_availability_from_text(
                text
            )

        # Price from text is intentionally last because
        # arbitrary numbers are dangerous to interpret as prices.
        if price is None:
            price = self._detect_price_from_text(
                text,
                currency,
            )

        # -----------------------------------------------------
        # Page metadata
        # -----------------------------------------------------

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
            attrs={
                "rel": re.compile(
                    r"canonical",
                    re.I,
                )
            },
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

    # =========================================================
    # JSON EXTRACTION
    # =========================================================

    def _extract_json(
        self,
        raw_payload: str,
    ) -> dict[str, Any]:

        try:
            data = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            return {
                "price": None,
                "currency": None,
                "availability": None,
                "attributes": {},
            }

        return self._extract_from_structured_data(
            data
        )

    def _extract_jsonld(
        self,
        soup: BeautifulSoup,
    ) -> Any:

        objects: list[Any] = []

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

            raw = script.string or script.get_text()

            if not raw:
                continue

            raw = raw.strip()

            try:
                objects.append(
                    json.loads(raw)
                )
            except (json.JSONDecodeError, TypeError):
                continue

        if not objects:
            return None

        return objects

    def _extract_from_structured_data(
        self,
        data: Any,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        attributes: dict[str, Any] = {}

        for obj in self._walk_json(data):

            if not isinstance(obj, dict):
                continue

            # -------------------------------------------------
            # Price
            # -------------------------------------------------

            if price is None:

                for key in (
                    "price",
                    "lowPrice",
                    "highPrice",
                    "amount",
                    "value",
                ):

                    if key not in obj:
                        continue

                    candidate = self._parse_price(
                        obj.get(key)
                    )

                    if candidate is not None:
                        price = candidate
                        break

            # -------------------------------------------------
            # Currency
            # -------------------------------------------------

            if currency is None:

                for key in (
                    "priceCurrency",
                    "currency",
                    "currencyCode",
                ):

                    value = obj.get(key)

                    detected = self._normalize_currency(
                        value
                    )

                    if detected:
                        currency = detected
                        break

            # -------------------------------------------------
            # Availability
            # -------------------------------------------------

            if availability is None:

                for key in (
                    "availability",
                    "availabilityStatus",
                    "stock",
                    "stockStatus",
                ):

                    value = obj.get(key)

                    detected = self._normalize_availability(
                        value
                    )

                    if detected:
                        availability = detected
                        break

            # -------------------------------------------------
            # Useful generic fields
            # -------------------------------------------------

            for key in (
                "name",
                "brand",
                "sku",
                "mpn",
                "model",
                "description",
            ):

                value = obj.get(key)

                if isinstance(value, (str, int, float)):
                    attributes.setdefault(
                        key,
                        value,
                    )

        return {
            "price": price,
            "currency": currency,
            "availability": availability,
            "attributes": attributes,
        }

    # =========================================================
    # META / OPENGRAPH
    # =========================================================

    def _extract_from_meta(
        self,
        soup: BeautifulSoup,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        attributes: dict[str, Any] = {}

        meta_fields = {
            "price": [
                "product:price:amount",
                "og:price:amount",
                "price",
                "product-price",
            ],
            "currency": [
                "product:price:currency",
                "og:price:currency",
                "priceCurrency",
                "currency",
            ],
            "availability": [
                "product:availability",
                "availability",
                "stock",
            ],
        }

        for field, names in meta_fields.items():

            for name in names:

                element = soup.find(
                    "meta",
                    attrs={
                        "property": name
                    },
                )

                if not element:
                    element = soup.find(
                        "meta",
                        attrs={
                            "name": name
                        },
                    )

                if not element:
                    continue

                value = element.get(
                    "content"
                )

                if not value:
                    continue

                if field == "price" and price is None:
                    price = self._parse_price(
                        value
                    )

                elif (
                    field == "currency"
                    and currency is None
                ):
                    currency = self._normalize_currency(
                        value
                    )

                elif (
                    field == "availability"
                    and availability is None
                ):
                    availability = self._normalize_availability(
                        value
                    )

        return {
            "price": price,
            "currency": currency,
            "availability": availability,
            "attributes": attributes,
        }

    # =========================================================
    # EMBEDDED JSON
    # =========================================================

    def _extract_embedded_json(
        self,
        soup: BeautifulSoup,
    ) -> dict[str, Any]:

        result = {
            "price": None,
            "currency": None,
            "availability": None,
            "attributes": {},
        }

        scripts = soup.find_all(
            "script"
        )

        for script in scripts:

            script_type = (
                script.get("type") or ""
            ).lower()

            if "json" not in script_type:
                continue

            raw = script.string or script.get_text()

            if not raw:
                continue

            try:
                data = json.loads(
                    raw.strip()
                )
            except (
                json.JSONDecodeError,
                TypeError,
            ):
                continue

            extracted = self._extract_from_structured_data(
                data
            )

            if result["price"] is None:
                result["price"] = extracted["price"]

            if result["currency"] is None:
                result["currency"] = extracted[
                    "currency"
                ]

            if result["availability"] is None:
                result["availability"] = extracted[
                    "availability"
                ]

            result["attributes"].update(
                extracted["attributes"]
            )

        return result

    # =========================================================
    # GENERIC HTML
    # =========================================================

    def _extract_from_html_selectors(
        self,
        soup: BeautifulSoup,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        attributes: dict[str, Any] = {}

        price_selectors = [
            '[itemprop="price"]',
            '[data-price]',
            '[data-product-price]',
            '[data-sale-price]',
            '[data-current-price]',
            '[class*="price"]',
            '[id*="price"]',
        ]

        for selector in price_selectors:

            for element in soup.select(
                selector
            ):

                value = (
                    element.get("content")
                    or element.get("data-price")
                    or element.get(
                        "data-product-price"
                    )
                    or element.get(
                        "data-sale-price"
                    )
                    or element.get(
                        "data-current-price"
                    )
                    or element.get_text(
                        " ",
                        strip=True,
                    )
                )

                candidate = self._parse_price(
                    value
                )

                if candidate is not None:
                    price = candidate
                    break

            if price is not None:
                break

        currency_element = soup.select_one(
            '[itemprop="priceCurrency"]'
        )

        if currency_element:

            value = (
                currency_element.get("content")
                or currency_element.get_text(
                    strip=True
                )
            )

            currency = self._normalize_currency(
                value
            )

        availability_selectors = [
            '[itemprop="availability"]',
            '[class*="availability"]',
            '[id*="availability"]',
            '[class*="stock"]',
            '[id*="stock"]',
        ]

        for selector in availability_selectors:

            for element in soup.select(
                selector
            ):

                value = (
                    element.get("href")
                    or element.get("content")
                    or element.get_text(
                        " ",
                        strip=True,
                    )
                )

                detected = self._normalize_availability(
                    value
                )

                if detected:
                    availability = detected
                    break

            if availability:
                break

        return {
            "price": price,
            "currency": currency,
            "availability": availability,
            "attributes": attributes,
        }

    # =========================================================
    # TEXT DETECTION
    # =========================================================

    @staticmethod
    def _detect_currency_from_text(
        text: str,
    ) -> Optional[str]:

        patterns = [
            (r"\bUSD\b", "USD"),
            (r"\bEUR\b", "EUR"),
            (r"\bGBP\b", "GBP"),
            (r"\bINR\b", "INR"),
            (r"\bJPY\b", "JPY"),
            (r"\bCAD\b", "CAD"),
            (r"\bAUD\b", "AUD"),
            (r"\bCHF\b", "CHF"),
            (r"\bCNY\b", "CNY"),
            (r"\bSGD\b", "SGD"),
            (r"\$", "USD"),
            (r"€", "EUR"),
            (r"£", "GBP"),
            (r"₹", "INR"),
            (r"¥", "JPY"),
        ]

        for pattern, currency in patterns:

            if re.search(
                pattern,
                text,
                re.IGNORECASE,
            ):
                return currency

        return None

    @staticmethod
    def _detect_availability_from_text(
        text: str,
    ) -> Optional[str]:

        normalized = text.lower()

        states = [
            (
                [
                    "discontinued",
                    "no longer available",
                ],
                "DISCONTINUED",
            ),
            (
                [
                    "backorder",
                    "back order",
                    "back-ordered",
                ],
                "BACKORDER",
            ),
            (
                [
                    "preorder",
                    "pre-order",
                    "pre order",
                ],
                "PREORDER",
            ),
            (
                [
                    "out of stock",
                    "out-of-stock",
                    "outofstock",
                    "sold out",
                    "currently unavailable",
                ],
                "OUT_OF_STOCK",
            ),
            (
                [
                    "in stock",
                    "in-stock",
                    "instock",
                ],
                "IN_STOCK",
            ),
        ]

        for phrases, state in states:

            if any(
                phrase in normalized
                for phrase in phrases
            ):
                return state

        return None

    @staticmethod
    def _detect_price_from_text(
        text: str,
        currency: Optional[str],
    ) -> Optional[Decimal]:

        if not currency:
            return None

        patterns: list[str] = []

        if currency == "USD":
            patterns = [
                r"\$\s*\d[\d,]*(?:\.\d{1,4})?",
                r"\bUSD\s*\d[\d,]*(?:\.\d{1,4})?",
            ]

        elif currency == "EUR":
            patterns = [
                r"€\s*\d[\d.,]*",
                r"\bEUR\s*\d[\d.,]*",
            ]

        elif currency == "GBP":
            patterns = [
                r"£\s*\d[\d,]*(?:\.\d{1,4})?",
                r"\bGBP\s*\d[\d,]*(?:\.\d{1,4})?",
            ]

        elif currency == "INR":
            patterns = [
                r"₹\s*\d[\d,]*(?:\.\d{1,4})?",
                r"\bINR\s*\d[\d,]*(?:\.\d{1,4})?",
            ]

        elif currency == "JPY":
            patterns = [
                r"¥\s*\d[\d,]*",
                r"\bJPY\s*\d[\d,]*",
            ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if not match:
                continue

            value = match.group(0)

            parsed = CollectionService._parse_price(
                value
            )

            if parsed is not None:
                return parsed

        return None

    # =========================================================
    # NORMALIZATION
    # =========================================================

    @staticmethod
    def _normalize_currency(
        value: Any,
    ) -> Optional[str]:

        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        symbol_map = {
            "$": "USD",
            "€": "EUR",
            "£": "GBP",
            "₹": "INR",
            "¥": "JPY",
        }

        if text in symbol_map:
            return symbol_map[text]

        match = re.search(
            r"\b[A-Za-z]{3}\b",
            text,
        )

        if match:
            return match.group(0).upper()

        return None

    @staticmethod
    def _normalize_availability(
        value: Any,
    ) -> Optional[str]:

        if value is None:
            return None

        text = str(value).lower().strip()

        if not text:
            return None

        if (
            "discontinued" in text
            or "no longer available" in text
        ):
            return "DISCONTINUED"

        if (
            "backorder" in text
            or "back order" in text
            or "back-ordered" in text
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
            or "out-of-stock" in text
            or "sold out" in text
            or "unavailable" in text
        ):
            return "OUT_OF_STOCK"

        if (
            "instock" in text
            or "in stock" in text
            or "in-stock" in text
            or text == "available"
        ):
            return "IN_STOCK"

        return None

    @staticmethod
    def _parse_price(
        value: Any,
    ) -> Optional[Decimal]:

        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        # Remove currency names/symbols while preserving
        # digits, comma, period and minus.
        text = re.sub(
            r"[^\d,.\-]",
            "",
            text,
        )

        if not text:
            return None

        # Reject multiple minus signs / invalid negatives.
        if text.count("-") > 1:
            return None

        if "-" in text and not text.startswith("-"):
            text = text.replace("-", "")

        # International formats:
        #
        # 399.99
        # 399,99
        # 1,299.99
        # 1.299,99
        #
        if "," in text and "." in text:

            if text.rfind(",") > text.rfind("."):

                # 1.299,99
                text = text.replace(".", "")
                text = text.replace(",", ".")

            else:

                # 1,299.99
                text = text.replace(",", "")

        elif "," in text:

            parts = text.split(",")

            if len(parts) == 2 and len(parts[-1]) in (
                1,
                2,
            ):
                # 399,99
                text = (
                    parts[0]
                    + "."
                    + parts[1]
                )
            else:
                # 1,299
                text = text.replace(",", "")

        try:

            price = Decimal(text)

            if price < 0:
                return None

            return price

        except InvalidOperation:
            return None

    # =========================================================
    # HELPERS
    # =========================================================

    @staticmethod
    def _walk_json(
        value: Any,
    ):
        """
        Recursively walk arbitrary JSON-like structures.
        """

        yield value

        if isinstance(value, dict):

            for child in value.values():
                yield from CollectionService._walk_json(
                    child
                )

        elif isinstance(value, list):

            for child in value:
                yield from CollectionService._walk_json(
                    child
                )

    @staticmethod
    def _is_json_content(
        content_type: Optional[str],
        body: str,
    ) -> bool:

        if content_type:
            if "application/json" in content_type.lower():
                return True

            if "+json" in content_type.lower():
                return True

        stripped = body.lstrip()

        return stripped.startswith(
            "{"
        ) or stripped.startswith(
            "["
        )

    @staticmethod
    def _determine_extraction_status(
        extracted: dict[str, Any],
    ) -> str:

        price = extracted.get("price")
        currency = extracted.get("currency")
        availability = extracted.get("availability")

        if (
            price is not None
            and currency is not None
            and availability is not None
        ):
            return "COMPLETE"

        if (
            price is not None
            or currency is not None
            or availability is not None
        ):
            return "PARTIAL"

        return "NO_DATA"

    @staticmethod
    def _looks_like_dynamic_page(
        soup: BeautifulSoup,
        html: str,
    ) -> bool:

        text = soup.get_text(
            " ",
            strip=True,
        ).lower()

        dynamic_phrases = [
            "enable javascript",
            "javascript is required",
            "loading...",
            "loading",
            "please wait",
            "rendering",
        ]

        if any(
            phrase in text
            for phrase in dynamic_phrases
        ):
            return True

        scripts = soup.find_all(
            "script"
        )

        if len(scripts) > 20 and len(
            soup.get_text(
                " ",
                strip=True
            )
        ) < 500:
            return True

        return False

    @staticmethod
    def _looks_like_challenge(
        soup: BeautifulSoup,
        html: str,
    ) -> bool:

        text = (
            soup.get_text(
                " ",
                strip=True,
            )
            .lower()
        )

        challenge_phrases = [
            "verify you are human",
            "checking your browser",
            "security check",
            "access denied",
            "unusual traffic",
            "captcha",
            "robot check",
            "bot detection",
        ]

        return any(
            phrase in text
            for phrase in challenge_phrases
        )
