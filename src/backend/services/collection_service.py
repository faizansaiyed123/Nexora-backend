from __future__ import annotations

import json
import re
import time
import asyncio
import random
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urljoin

from backend.services.url_security import SecurityValidationError, UrlSecurityService
from backend.services.pinned_http import PinnedDNSHTTPTransport
from backend.core.error_messages import safe_collection_error
from backend.core.config import get_settings
from backend.collectors.universal_extractor import UniversalExtractor, get_site_preset

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("nexora.collection")
_settings = get_settings()


@dataclass
class CollectionResult:
    """
    Generic collection result.

    Only price, currency and availability are treated as
    canonical competitive-intelligence fields.

    All other discovered information is stored dynamically
    inside `attributes`.
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
        Response inspection
          ↓
        JSON / JSON-LD
          ↓
        Meta / OpenGraph
          ↓
        Embedded JSON
          ↓
        Microdata
          ↓
        Generic HTML selectors
          ↓
        Generic text detection
          ↓
        CollectionResult

    No website-specific selectors.
    No hardcoded product attributes.
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

    # ------------------------------------------------------------------
    # Retry / backoff helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
        """Exponential backoff with full jitter.

        Full jitter (random between 0 and the exponential value) is used
        instead of equal jitter because it spreads retries more evenly across
        a worker fleet and avoids thundering-herd synchronisation on the
        target server.
        """
        if attempt <= 0:
            return 0.0
        exponential = min(cap, base * (2 ** (attempt - 1)))
        return random.uniform(0.0, exponential)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 425, 429, 500, 502, 503, 504}

    @staticmethod
    def _is_retryable_exception(exc: BaseException) -> bool:
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, httpx.ConnectError):
            return True
        if isinstance(exc, httpx.ReadError):
            return True
        if isinstance(exc, httpx.WriteError):
            return True
        if isinstance(exc, httpx.RemoteProtocolError):
            return True
        if isinstance(exc, httpx.ConnectTimeout):
            return True
        return False

    # =========================================================
    # PUBLIC API
    # =========================================================

    async def collect(
        self,
        url: str,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Optional[dict[str, str]] = None,
        custom_selectors: Optional[dict[str, Any]] = None,
        max_retries: int = 0,
    ) -> CollectionResult:
        """
        Fetch URL and perform generic extraction.
        """

        try:
            current_url = UrlSecurityService.validate_url(url)
        except SecurityValidationError as exc:
            return CollectionResult(False, url, None, 0, None, None, None, {}, extraction_status="SECURITY_BLOCKED", error=safe_collection_error(exc))

        request_headers = self.DEFAULT_HEADERS.copy()

        if headers:
            request_headers.update(headers)

        start_time = time.perf_counter()

        try:
            current_attempt = 0
            response = None
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=PinnedDNSHTTPTransport(),
                trust_env=False,
            ) as client:
                while current_attempt <= max(0, max_retries):
                    response = None
                    current_url = UrlSecurityService.validate_url(url)
                    try:
                        for _ in range(6):
                            response = await client.get(
                                current_url,
                                headers=request_headers,
                            )
                            if response.status_code not in {301, 302, 303, 307, 308}:
                                break
                            location = response.headers.get("location")
                            if not location:
                                break
                            try:
                                current_url = UrlSecurityService.validate_url(
                                    urljoin(current_url, location)
                                )
                            except SecurityValidationError as exc:
                                return CollectionResult(
                                    success=False,
                                    url=current_url,
                                    status_code=response.status_code,
                                    response_time_ms=int(
                                        (time.perf_counter() - start_time) * 1000
                                    ),
                                    price=None,
                                    currency=None,
                                    availability=None,
                                    attributes={"redirect_blocked": True},
                                    extraction_status="SECURITY_BLOCKED",
                                    error=safe_collection_error(exc),
                                )

                        if response is None:
                            raise httpx.HTTPError("No HTTP response received")

                        retryable_status = self._is_retryable_status(response.status_code)
                        if retryable_status and current_attempt < max(0, max_retries):
                            current_attempt += 1
                            await asyncio.sleep(self._backoff_delay(current_attempt))
                            continue

                        break
                    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as exc:
                        if not self._is_retryable_exception(exc) or current_attempt >= max(0, max_retries):
                            raise
                        current_attempt += 1
                        await asyncio.sleep(self._backoff_delay(current_attempt))

                final_url = current_url
            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            final_url = current_url

            content_type = response.headers.get(
                "content-type"
            )

            raw_payload = response.text

            content_length = len(response.content)
            if content_length > 5 * 1024 * 1024:
                return CollectionResult(
                    success=False,
                    url=final_url,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    price=None,
                    currency=None,
                    availability=None,
                    attributes={},
                    extraction_status="RESPONSE_TOO_LARGE",
                    error="Response body exceeds the maximum allowed size.",
                )

            # =================================================
            # HTTP ERROR
            # =================================================

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

            # =================================================
            # JSON RESPONSE
            # =================================================

            if self._is_json_content(
                content_type,
                raw_payload,
            ):

                extracted = self._extract_json(
                    raw_payload
                )

                extraction_status = (
                    self._determine_extraction_status(
                        extracted
                    )
                )

                attributes = {
                    **extracted["attributes"],
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "content_length": content_length,
                }

                success = (
                    extraction_status != "NO_DATA"
                )

                return CollectionResult(
                    success=success,
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
                    error=(
                        None
                        if success
                        else (
                            "HTTP request succeeded but "
                            "no useful competitive data "
                            "was extracted."
                        )
                    ),
                )

            # =================================================
            # HTML RESPONSE
            # =================================================

            soup = BeautifulSoup(
                raw_payload,
                "html.parser",
            )

            # Production pipeline: UniversalExtractor first, then legacy extractor.
            extractor = UniversalExtractor()
            ue_result = extractor.extract(
                raw_payload, final_url, custom_selectors=custom_selectors
            )

            price = ue_result.price
            currency = ue_result.currency
            availability = ue_result.availability
            attributes = dict(ue_result.attributes)

            # Fallback to legacy extractor if UniversalExtractor found nothing useful.
            if price is None and availability in (None, "UNKNOWN"):
                legacy = self._extract_html(
                    soup=soup,
                    html=raw_payload,
                    base_url=final_url,
                    custom_selectors=custom_selectors,
                )
                if price is None:
                    price = legacy["price"]
                if currency is None:
                    currency = legacy["currency"]
                if availability in (None, "UNKNOWN"):
                    availability = legacy["availability"]
                attributes = {**legacy["attributes"], **attributes}

            # Merge discovered product fields into attributes.
            for field_name in ("name", "sku", "brand", "category", "image", "description"):
                value = getattr(ue_result, field_name, None)
                if value:
                    attributes.setdefault(field_name, value)

            if ue_result.strategy_used != "NONE":
                attributes["extraction_strategy"] = ue_result.strategy_used
                attributes["extraction_confidence"] = ue_result.confidence

            extraction_status = (
                self._determine_extraction_status(
                    {
                        "price": price,
                        "currency": currency,
                        "availability": availability,
                    }
                )
            )

            attributes = {
                **attributes,
                "http_status": response.status_code,
                "content_type": content_type,
                "content_length": content_length,
            }

            # =================================================
            # PAGE METADATA
            # =================================================

            title = soup.find("title")

            if title:
                attributes["page_title"] = (
                    title.get_text(
                        " ",
                        strip=True,
                    )
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

            if (
                description
                and description.get("content")
            ):
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

            if (
                canonical
                and canonical.get("href")
            ):
                attributes["canonical_url"] = urljoin(
                    final_url,
                    canonical["href"],
                )

            # =================================================
            # DYNAMIC PAGE DIAGNOSTICS
            # =================================================

            if response.status_code == 202:

                attributes["http_202"] = True

                if not raw_payload.strip():
                    attributes["empty_response"] = True

                if self._looks_like_dynamic_page(
                    soup,
                    raw_payload,
                ):
                    attributes[
                        "possible_dynamic_rendering"
                    ] = True

                if self._looks_like_challenge(
                    soup,
                    raw_payload,
                ):
                    attributes[
                        "possible_challenge"
                    ] = True

            # =================================================
            # AUTO-RENDER FALLBACK (best-effort)
            # =================================================
            #
            # When deterministic extraction produced nothing useful and the
            # page looks JS-rendered or bot-challenged, transparently retry with
            # a headless browser. This is opt-out via settings.auto_render_enabled
            # and never blocks the caller when Playwright is unavailable.

            if (
                extraction_status in ("NO_DATA", "PARTIAL", "TIMEOUT", "HTTP_ERROR")
                and _settings.auto_render_enabled
                and (
                    attributes.get("possible_dynamic_rendering")
                    or attributes.get("possible_challenge")
                    or self._looks_like_dynamic_page(soup, raw_payload)
                    or self._looks_like_challenge(soup, raw_payload)
                )
            ):
                rendered = await self._try_render(final_url, timeout_seconds=int(timeout))
                if rendered is not None:
                    rendered_result = self.from_rendered_html(
                        rendered,
                        final_url,
                        response_time_ms=response_time_ms,
                        status_code=response.status_code,
                        custom_selectors=custom_selectors,
                    )
                    rendered_result.attributes["auto_rendered"] = True
                    rendered_result.attributes["collection_engine"] = "PLAYWRIGHT_AUTO_RENDER"
                    return rendered_result

            success = (
                extraction_status != "NO_DATA"
            )

            return CollectionResult(
                success=success,
                url=final_url,
                status_code=response.status_code,
                response_time_ms=response_time_ms,
                price=price,
                currency=currency,
                availability=availability,
                attributes=attributes,
                raw_payload=raw_payload,
                content_type=content_type,
                content_length=content_length,
                extraction_status=extraction_status,
                error=(
                    None
                    if success
                    else (
                        "HTTP request succeeded but "
                        "no useful competitive data "
                        "was extracted."
                    )
                ),
            )

        # =====================================================
        # TIMEOUT
        # =====================================================

        except httpx.TimeoutException:

            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            # Try auto-render fallback for slow/JS-heavy sites.
            rendered = await self._try_render(url, timeout_seconds=max(int(timeout) * 3, 30))
            if rendered is not None:
                rendered_result = self.from_rendered_html(
                    rendered,
                    url,
                    response_time_ms=response_time_ms,
                    status_code=200,
                    custom_selectors=custom_selectors,
                )
                rendered_result.attributes["auto_rendered"] = True
                rendered_result.attributes["collection_engine"] = "PLAYWRIGHT_AUTO_RENDER"
                return rendered_result

            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=response_time_ms,
                price=None,
                currency=None,
                availability=None,
                attributes={
                    "possible_dynamic_rendering": True,
                    "possible_challenge": False,
                },
                extraction_status="TIMEOUT",
                error="Request timeout",
            )

        # =====================================================
        # HTTP ERROR
        # =====================================================

        except httpx.HTTPError as exc:

            response_time_ms = int(
                (time.perf_counter() - start_time) * 1000
            )

            # Try auto-render fallback for HTTP errors that may be bot challenges.
            rendered = await self._try_render(url, timeout_seconds=max(int(timeout) * 3, 30))
            if rendered is not None:
                rendered_result = self.from_rendered_html(
                    rendered,
                    url,
                    response_time_ms=response_time_ms,
                    status_code=200,
                    custom_selectors=custom_selectors,
                )
                rendered_result.attributes["auto_rendered"] = True
                rendered_result.attributes["collection_engine"] = "PLAYWRIGHT_AUTO_RENDER"
                if not rendered_result.success:
                    rendered_result.error = safe_collection_error(exc)
                return rendered_result

            return CollectionResult(
                success=False,
                url=url,
                status_code=None,
                response_time_ms=response_time_ms,
                price=None,
                currency=None,
                availability=None,
                attributes={},
                extraction_status="HTTP_ERROR",
                error=safe_collection_error(exc),
            )

        # =====================================================
        # UNEXPECTED ERROR
        # =====================================================

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
                extraction_status="ERROR",
                error=str(exc),
            )

    def from_rendered_html(
        self,
        html: str,
        url: str,
        *,
        response_time_ms: int = 0,
        status_code: int = 200,
        custom_selectors: Optional[dict[str, Any]] = None,
    ) -> CollectionResult:
        """Extract competitive data from HTML already rendered by a browser."""
        # Production pipeline: UniversalExtractor first, then legacy extractor.
        extractor = UniversalExtractor()
        ue_result = extractor.extract(html, url, custom_selectors=custom_selectors)

        price = ue_result.price
        currency = ue_result.currency
        availability = ue_result.availability
        attributes = dict(ue_result.attributes)

        # Fallback to legacy extractor if UniversalExtractor found nothing useful.
        if price is None and availability in (None, "UNKNOWN"):
            soup = BeautifulSoup(html, "html.parser")
            legacy = self._extract_html(
                soup=soup,
                html=html,
                base_url=url,
                custom_selectors=custom_selectors,
            )
            if price is None:
                price = legacy["price"]
            if currency is None:
                currency = legacy["currency"]
            if availability in (None, "UNKNOWN"):
                availability = legacy["availability"]
            attributes = {**legacy["attributes"], **attributes}

        # Merge discovered product fields into attributes.
        for field_name in ("name", "sku", "brand", "category", "image", "description"):
            value = getattr(ue_result, field_name, None)
            if value:
                attributes.setdefault(field_name, value)

        if ue_result.strategy_used != "NONE":
            attributes["extraction_strategy"] = ue_result.strategy_used
            attributes["extraction_confidence"] = ue_result.confidence

        extraction_status = self._determine_extraction_status(
            {"price": price, "currency": currency, "availability": availability}
        )
        success = price is not None or availability not in (None, "UNKNOWN")
        return CollectionResult(
            success=success,
            url=url,
            status_code=status_code,
            response_time_ms=response_time_ms,
            price=price,
            currency=currency,
            availability=availability,
            attributes=attributes,
            raw_payload=html,
            content_type="text/html",
            content_length=len(html.encode("utf-8")),
            extraction_status=extraction_status,
            error=None if success else "Rendered page contained no useful competitive data.",
        )

    async def _try_render(self, url: str, timeout_seconds: int = 20) -> Optional[str]:
        """Best-effort Playwright render. Returns None when unavailable."""
        try:
            from backend.collectors.browser import render_page

            return await asyncio.wait_for(
                render_page(url, timeout_seconds=timeout_seconds),
                timeout=timeout_seconds + 15,
            )
        except asyncio.TimeoutError:
            logger.info("Auto-render timed out for %s", url)
            return None
        except Exception as exc:
            logger.info("Auto-render skipped for %s: %s", url, exc)
            return None

    # =========================================================
    # HTML EXTRACTION
    # =========================================================

    def _extract_html(
        self,
        soup: BeautifulSoup,
        html: str,
        base_url: str,
        custom_selectors: Optional[dict[str, Any]] = None,
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

            jsonld_result = (
                self._extract_from_structured_data(
                    jsonld_data
                )
            )

            if price is None:
                price = jsonld_result["price"]

            if currency is None:
                currency = jsonld_result[
                    "currency"
                ]

            if availability is None:
                availability = jsonld_result[
                    "availability"
                ]

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
            availability = meta_result[
                "availability"
            ]

        attributes.update(
            meta_result["attributes"]
        )

        # -----------------------------------------------------
        # 3. Embedded JSON
        # -----------------------------------------------------

        embedded_result = (
            self._extract_embedded_json(
                soup
            )
        )

        if price is None:
            price = embedded_result["price"]

        if currency is None:
            currency = embedded_result[
                "currency"
            ]

        if availability is None:
            availability = embedded_result[
                "availability"
            ]

        attributes.update(
            embedded_result["attributes"]
        )

        # -----------------------------------------------------
        # 4. Microdata
        # -----------------------------------------------------

        microdata_result = (
            self._extract_microdata(
                soup
            )
        )

        if price is None:
            price = microdata_result["price"]

        if currency is None:
            currency = microdata_result[
                "currency"
            ]

        if availability is None:
            availability = microdata_result[
                "availability"
            ]

        attributes.update(
            microdata_result["attributes"]
        )

        # -----------------------------------------------------
        # 5. Generic HTML selectors
        # -----------------------------------------------------

        html_result = (
            self._extract_from_html_selectors(
                soup,
                custom_selectors=custom_selectors,
            )
        )

        if price is None:
            price = html_result["price"]

        if currency is None:
            currency = html_result["currency"]

        if availability is None:
            availability = html_result[
                "availability"
            ]

        attributes.update(
            html_result["attributes"]
        )

        # -----------------------------------------------------
        # 6. Dynamic HTML attributes
        # -----------------------------------------------------

        dynamic_result = (
            self._extract_dynamic_html_attributes(
                soup
            )
        )

        attributes.update(
            dynamic_result
        )

        # -----------------------------------------------------
        # 7. Generic page text
        # -----------------------------------------------------

        text = soup.get_text(
            " ",
            strip=True,
        )

        if currency is None:
            currency = (
                self._detect_currency_from_text(
                    text
                )
            )

        if availability is None:
            availability = (
                self._detect_availability_from_text(
                    text
                )
            )

        if price is None:
            price = self._detect_price_from_text(
                text,
                currency,
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
            data = json.loads(
                raw_payload
            )

        except (
            json.JSONDecodeError,
            TypeError,
        ):

            return {
                "price": None,
                "currency": None,
                "availability": None,
                "attributes": {},
            }

        return self._extract_from_structured_data(
            data
        )

    # =========================================================
    # JSON-LD
    # =========================================================

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

            raw = (
                script.string
                or script.get_text()
            )

            if not raw:
                continue

            raw = raw.strip()

            try:

                objects.append(
                    json.loads(raw)
                )

            except (
                json.JSONDecodeError,
                TypeError,
            ):
                continue

        if not objects:
            return None

        return objects

    # =========================================================
    # STRUCTURED DATA
    # =========================================================

    def _extract_from_structured_data(
        self,
        data: Any,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        attributes: dict[str, Any] = {}

        # Preserve the original structured data.
        #
        # This is dynamic and does not assume any
        # product attribute names.
        attributes["structured_data"] = data

        for obj in self._walk_json(data):

            if not isinstance(obj, dict):
                continue

            # -------------------------------------------------
            # PRICE
            # -------------------------------------------------

            if price is None:

                for key in (
                    "price",
                    "lowPrice",
                    "highPrice",
                ):

                    if key not in obj:
                        continue

                    candidate = (
                        self._parse_price(
                            obj.get(key)
                        )
                    )

                    if (
                        candidate is not None
                        and candidate > 0
                    ):
                        price = candidate
                        break

            # -------------------------------------------------
            # CURRENCY
            # -------------------------------------------------

            if currency is None:

                for key in (
                    "priceCurrency",
                    "currency",
                    "currencyCode",
                ):

                    detected = (
                        self._normalize_currency(
                            obj.get(key)
                        )
                    )

                    if detected:
                        currency = detected
                        break

            # -------------------------------------------------
            # AVAILABILITY
            # -------------------------------------------------

            if availability is None:

                for key in (
                    "availability",
                    "availabilityStatus",
                    "stock",
                    "stockStatus",
                ):

                    detected = (
                        self._normalize_availability(
                            obj.get(key)
                        )
                    )

                    if detected:
                        availability = detected
                        break

            # -------------------------------------------------
            # Dynamic attributes
            # -------------------------------------------------

            #
            # We do NOT do:
            #
            # if key == "brand"
            # if key == "sku"
            # if key == "name"
            #
            # Instead, all structured data is preserved above.
            #

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

        # Store ALL meta tags dynamically.
        meta_attributes: dict[str, Any] = {}

        for element in soup.find_all(
            "meta"
        ):

            key = (
                element.get("property")
                or element.get("name")
                or element.get("itemprop")
            )

            value = element.get(
                "content"
            )

            if not key or value is None:
                continue

            key = str(key).strip()

            meta_attributes[key] = value

        attributes["meta"] = meta_attributes

        # -----------------------------------------------------
        # Detect price from known metadata conventions
        # -----------------------------------------------------

        price_fields = [
            "product:price:amount",
            "og:price:amount",
            "price",
            "product-price",
        ]

        for name in price_fields:

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

            price = self._parse_price(
                value
            )

            if price is not None:
                break

        # -----------------------------------------------------
        # Currency
        # -----------------------------------------------------

        currency_fields = [
            "product:price:currency",
            "og:price:currency",
            "priceCurrency",
            "currency",
        ]

        for name in currency_fields:

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

            currency = (
                self._normalize_currency(
                    value
                )
            )

            if currency:
                break

        # -----------------------------------------------------
        # Availability
        # -----------------------------------------------------

        availability_fields = [
            "product:availability",
            "availability",
            "stock",
        ]

        for name in availability_fields:

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

            availability = (
                self._normalize_availability(
                    value
                )
            )

            if availability:
                break

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

        embedded_objects: list[Any] = []

        for script in scripts:

            script_type = (
                script.get("type")
                or ""
            ).lower()

            if (
                "json" not in script_type
                or "ld+json" in script_type
            ):
                continue

            raw = (
                script.string
                or script.get_text()
            )

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

            embedded_objects.append(
                data
            )

            extracted = (
                self._extract_from_structured_data(
                    data
                )
            )

            if result["price"] is None:
                result["price"] = (
                    extracted["price"]
                )

            if result["currency"] is None:
                result["currency"] = (
                    extracted["currency"]
                )

            if result["availability"] is None:
                result["availability"] = (
                    extracted["availability"]
                )

        if embedded_objects:
            result["attributes"][
                "embedded_json"
            ] = embedded_objects

        return result

    # =========================================================
    # MICRODATA
    # =========================================================

    def _extract_microdata(
        self,
        soup: BeautifulSoup,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        microdata: dict[str, Any] = {}

        elements = soup.select(
            "[itemprop]"
        )

        for element in elements:

            key = element.get(
                "itemprop"
            )

            if not key:
                continue

            value = (
                element.get("content")
                or element.get("value")
                or element.get("href")
                or element.get_text(
                    " ",
                    strip=True,
                )
            )

            if value is None:
                continue

            # Preserve dynamically.
            existing = microdata.get(
                key
            )

            if existing is None:

                microdata[key] = value

            elif isinstance(existing, list):

                existing.append(value)

            else:

                microdata[key] = [
                    existing,
                    value,
                ]

            # ---------------------------------------------
            # Only canonical fields are interpreted.
            # ---------------------------------------------

            if (
                key == "price"
                and price is None
            ):
                price = self._parse_price(
                    value
                )

            elif (
                key == "priceCurrency"
                and currency is None
            ):
                currency = (
                    self._normalize_currency(
                        value
                    )
                )

            elif (
                key == "availability"
                and availability is None
            ):
                availability = (
                    self._normalize_availability(
                        value
                    )
                )

        return {
            "price": price,
            "currency": currency,
            "availability": availability,
            "attributes": {
                "microdata": microdata
            },
        }

    # =========================================================
    # GENERIC HTML SELECTORS
    # =========================================================

    def _extract_from_html_selectors(
        self,
        soup: BeautifulSoup,
        custom_selectors: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:

        price: Optional[Decimal] = None
        currency: Optional[str] = None
        availability: Optional[str] = None

        attributes: dict[str, Any] = {}

        # -----------------------------------------------------
        # PRICE
        # -----------------------------------------------------

        configured_price = custom_selectors.get("price_selector") if isinstance(custom_selectors, dict) else None
        configured_currency = custom_selectors.get("currency_selector") if isinstance(custom_selectors, dict) else None
        configured_availability = custom_selectors.get("availability_selector") if isinstance(custom_selectors, dict) else None

        price_selectors = [
            *([str(configured_price)] if configured_price else []),
            '[itemprop="price"]',
            "[data-price]",
            "[data-product-price]",
            "[data-sale-price]",
            "[data-current-price]",
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

                candidate = (
                    self._parse_price(
                        value
                    )
                )

                if candidate is not None:

                    price = candidate
                    break

            if price is not None:
                break

        # -----------------------------------------------------
        # CURRENCY
        # -----------------------------------------------------

        currency_element = soup.select_one(str(configured_currency)) if configured_currency else soup.select_one('[itemprop="priceCurrency"]')

        if currency_element:

            value = (
                currency_element.get(
                    "content"
                )
                or currency_element.get_text(
                    strip=True
                )
            )

            currency = (
                self._normalize_currency(
                    value
                )
            )

        # -----------------------------------------------------
        # AVAILABILITY
        # -----------------------------------------------------

        availability_selectors = [
            *([str(configured_availability)] if configured_availability else []),
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

                detected = (
                    self._normalize_availability(
                        value
                    )
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
    # DYNAMIC HTML ATTRIBUTES
    # =========================================================

    def _extract_dynamic_html_attributes(
        self,
        soup: BeautifulSoup,
    ) -> dict[str, Any]:

        result: dict[str, Any] = {}

        # -----------------------------------------------------
        # HTML data-* attributes
        # -----------------------------------------------------

        data_attributes: dict[str, Any] = {}

        for element in soup.find_all():

            for key, value in element.attrs.items():

                if not key.startswith(
                    "data-"
                ):
                    continue

                if isinstance(
                    value,
                    list,
                ):
                    value = " ".join(
                        value
                    )

                existing = data_attributes.get(
                    key
                )

                if existing is None:

                    data_attributes[key] = value

                elif isinstance(
                    existing,
                    list,
                ):

                    existing.append(value)

                else:

                    data_attributes[key] = [
                        existing,
                        value,
                    ]

        if data_attributes:

            result[
                "data_attributes"
            ] = data_attributes

        return result

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

            parsed = (
                CollectionService._parse_price(
                    match.group(0)
                )
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
            "C$": "CAD",
            "A$": "AUD",
            "CA$": "CAD",
            "AU$": "AUD",
        }

        if text in symbol_map:
            return symbol_map[text]

        iso_currencies = {
            "USD",
            "EUR",
            "GBP",
            "INR",
            "JPY",
            "CAD",
            "AUD",
            "CHF",
            "CNY",
            "SGD",
            "NZD",
            "AED",
            "SAR",
            "SEK",
            "NOK",
            "DKK",
            "HKD",
            "KRW",
            "BRL",
            "MXN",
            "ZAR",
            "TRY",
            "RUB",
            "PLN",
            "THB",
        }

        match = re.search(
            r"\b[A-Za-z]{3}\b",
            text,
        )

        if match:

            code = match.group(
                0
            ).upper()

            if code in iso_currencies:
                return code

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

        text = re.sub(
            r"[^\d,.\-]",
            "",
            text,
        )

        if not text:
            return None

        if text.count("-") > 1:
            return None

        if (
            "-"
            in text
            and not text.startswith("-")
        ):
            text = text.replace(
                "-",
                "",
            )

        # ---------------------------------------------
        # 1.299,99
        # ---------------------------------------------

        if "," in text and "." in text:

            if (
                text.rfind(",")
                > text.rfind(".")
            ):

                text = text.replace(
                    ".",
                    "",
                )

                text = text.replace(
                    ",",
                    ".",
                )

            else:

                text = text.replace(
                    ",",
                    "",
                )

        # ---------------------------------------------
        # 399,99
        # ---------------------------------------------

        elif "," in text:

            parts = text.split(",")

            if (
                len(parts) == 2
                and len(parts[-1]) in (1, 2)
            ):

                text = (
                    parts[0]
                    + "."
                    + parts[1]
                )

            else:

                text = text.replace(
                    ",",
                    "",
                )

        try:

            price = Decimal(
                text
            )

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
        Recursively walk arbitrary JSON-like data.
        """

        yield value

        if isinstance(
            value,
            dict,
        ):

            for child in value.values():

                yield from (
                    CollectionService._walk_json(
                        child
                    )
                )

        elif isinstance(
            value,
            list,
        ):

            for child in value:

                yield from (
                    CollectionService._walk_json(
                        child
                    )
                )

    @staticmethod
    def _is_json_content(
        content_type: Optional[str],
        body: str,
    ) -> bool:

        if content_type:

            content_type_lower = (
                content_type.lower()
            )

            if (
                "application/json"
                in content_type_lower
            ):
                return True

            if (
                "+json"
                in content_type_lower
            ):
                return True

        stripped = body.lstrip()

        return (
            stripped.startswith("{")
            or stripped.startswith("[")
        )

    @staticmethod
    def _determine_extraction_status(
        extracted: dict[str, Any],
    ) -> str:

        price = extracted.get(
            "price"
        )

        currency = extracted.get(
            "currency"
        )

        availability = extracted.get(
            "availability"
        )

        if (
            price is not None
            and currency is not None
            and availability is not None
        ):
            return "COMPLETE"

        if price is not None or availability not in (None, "UNKNOWN"):
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

        if (
            len(scripts) > 20
            and len(
                soup.get_text(
                    " ",
                    strip=True,
                )
            ) < 500
        ):
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
            "are you a human",
            "i'm not a robot",
            "just a moment",
            "moment please",
            "please wait while we verify",
            "ddos protection",
            "browser check",
            "attention required",
            "you have been blocked",
            "request blocked",
            "an error occurred",
            "cloudflare",
            "akamai",
            "datadome",
            "perimeterx",
            "incapsula",
            "sucuri",
            "site protection",
            "challenge-platform",
            "cf-chl",
            "jschallenge",
            "ddos-guard",
            "redirecting...",
            "please enable javascript",
        ]

        if any(
            phrase in text
            for phrase in challenge_phrases
        ):
            return True

        # CDN-specific markers in markup (case-insensitive).
        lower_html = html.lower()
        cdn_markers = [
            "cf-chl-bypass",
            "cf-chl-script",
            "cf-browser-verification",
            "datadome",
            "pxc2",
            "akamai",
            "incapsula",
            "sucuri",
            "perimeterx",
            "bot-detection",
            "jschallenge",
            "ddos-guard",
            "captcha-container",
            "g-recaptcha",
            "hcaptcha",
            "arkoselabs",
            "funcaptcha",
        ]
        return any(marker in lower_html for marker in cdn_markers)
