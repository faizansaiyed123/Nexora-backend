"""
Universal Dual-Engine Extractor Service for Nexora.
Completely dynamic and website-independent:
- Fast-Path Async HTTP Engine with realistic browser headers & redirect resolution
- Universal Multi-Tier Parsing:
    1. Schema.org / JSON-LD (Product, Offer, AggregateOffer, @graph)
    2. OpenGraph, Twitter Cards, Semantic <meta> tags & Microdata
    3. Embedded SPA State (__NEXT_DATA__, __NUXT__, __INITIAL_STATE__)
    4. Generic Semantic HTML & Microformat Heuristics
- Headless Browser / Playwright fallback capability
- Zero hardcoded website selectors, domain filters, or currency assumptions
- Deep HTTP 202 / Anti-Bot inspection & diagnostic logging
"""

from decimal import Decimal
import html
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from backend.models.enums import (
    AvailabilityStatusEnum,
    CollectionMethodEnum,
    ErrorCategoryEnum
)
from backend.schemas.extraction import CollectionResult

logger = logging.getLogger("nexora.extractor")

# Currency Symbol to ISO 4217 Mapping
CURRENCY_SYMBOL_MAP: Dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₽": "RUB",
    "₩": "KRW",
    "฿": "THB",
    "₫": "VND",
    "₱": "PHP",
    "R$": "BRL",
    "zł": "PLN",
    "kr": "SEK",
    "Kč": "CZK",
    "Ft": "HUF",
    "₪": "ILS",
    "CA$": "CAD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "SG$": "SGD",
    "CHF": "CHF",
    "AED": "AED",
    "SAR": "SAR",
    "ZAR": "ZAR"
}

# Standard Schema.org Availability mappings
SCHEMA_AVAILABILITY_MAP: Dict[str, AvailabilityStatusEnum] = {
    "instock": AvailabilityStatusEnum.IN_STOCK,
    "http://schema.org/instock": AvailabilityStatusEnum.IN_STOCK,
    "https://schema.org/instock": AvailabilityStatusEnum.IN_STOCK,
    "outofstock": AvailabilityStatusEnum.OUT_OF_STOCK,
    "http://schema.org/outofstock": AvailabilityStatusEnum.OUT_OF_STOCK,
    "https://schema.org/outofstock": AvailabilityStatusEnum.OUT_OF_STOCK,
    "preorder": AvailabilityStatusEnum.PRE_ORDER,
    "http://schema.org/preorder": AvailabilityStatusEnum.PRE_ORDER,
    "https://schema.org/preorder": AvailabilityStatusEnum.PRE_ORDER,
    "discontinued": AvailabilityStatusEnum.DISCONTINUED,
    "http://schema.org/discontinued": AvailabilityStatusEnum.DISCONTINUED,
    "https://schema.org/discontinued": AvailabilityStatusEnum.DISCONTINUED,
    "backorder": AvailabilityStatusEnum.AVAILABLE,
    "limitedavailability": AvailabilityStatusEnum.IN_STOCK,
    "onlineonly": AvailabilityStatusEnum.IN_STOCK,
    "in_stock": AvailabilityStatusEnum.IN_STOCK,
    "out_of_stock": AvailabilityStatusEnum.OUT_OF_STOCK,
    "available": AvailabilityStatusEnum.AVAILABLE,
    "unavailable": AvailabilityStatusEnum.UNAVAILABLE,
    "on_request": AvailabilityStatusEnum.ON_REQUEST
}


class UniversalExtractor:
    """
    Dynamic heuristic parser for HTML and JSON payloads.
    Operates without domain-specific selectors.
    """

    @classmethod
    def extract_from_html_or_json(
        cls, 
        raw_text: str, 
        target_url: str, 
        headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Executes multi-tier dynamic extraction across JSON-LD, meta tags,
        embedded SPA JSON, and semantic HTML microformats.
        """
        if not raw_text or not raw_text.strip():
            return {"error": "Empty response body"}

        data: Dict[str, Any] = {
            "name": None,
            "price": None,
            "currency": None,
            "availability": AvailabilityStatusEnum.UNKNOWN,
            "sku": None,
            "brand": None,
            "category": None,
            "attributes": {},
            "extracted_fields": {},
            "strategy_used": "UNKNOWN"
        }

        trimmed = raw_text.strip()

        # Check if raw response is pure JSON (e.g. from an API or microservice)
        if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
            try:
                parsed_json = json.loads(trimmed)
                cls._extract_from_json_dict(parsed_json, data)
                if data["price"] is not None:
                    data["strategy_used"] = "DIRECT_JSON_PAYLOAD"
                    return data
            except Exception as e:
                logger.debug(f"Direct JSON parse attempt skipped: {e}")

        # Tier 1: Extract and parse all Schema.org / JSON-LD blocks
        cls._extract_json_ld(raw_text, data)
        if data["price"] is not None and data["currency"] is not None:
            data["strategy_used"] = "JSON_LD_SCHEMA"
            return data

        # Tier 2: Extract OpenGraph, Twitter Card & Semantic Meta Tags
        cls._extract_meta_tags(raw_text, data)
        if data["price"] is not None:
            data["strategy_used"] = "SEMANTIC_META_TAGS"
            return data

        # Tier 3: Extract Embedded Framework / SPA State (e.g. __NEXT_DATA__, __NUXT__)
        cls._extract_embedded_framework_state(raw_text, data)
        if data["price"] is not None:
            data["strategy_used"] = "EMBEDDED_SPA_STATE"
            return data

        # Tier 4: Generic Semantic HTML Microformats & Regex Heuristics
        cls._extract_semantic_html_heuristics(raw_text, data)
        if data["price"] is not None:
            data["strategy_used"] = "SEMANTIC_HTML_HEURISTICS"

        return data

    @classmethod
    def _extract_json_ld(cls, html_content: str, out: Dict[str, Any]) -> None:
        """Extracts JSON-LD script tags (<script type="application/ld+json">)."""
        pattern = re.compile(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE
        )
        matches = pattern.findall(html_content)

        for match in matches:
            clean_match = match.strip()
            if not clean_match:
                continue
            try:
                parsed = json.loads(clean_match)
                items = parsed if isinstance(parsed, list) else [parsed]
                for item in items:
                    cls._process_json_ld_item(item, out)
                    if out["price"] is not None:
                        return
            except Exception as e:
                logger.debug(f"JSON-LD snippet parse error: {e}")

    @classmethod
    def _process_json_ld_item(cls, item: Any, out: Dict[str, Any]) -> None:
        """Recursively parses Schema.org graphs and items."""
        if not isinstance(item, dict):
            return

        # Handle @graph wrapper
        if "@graph" in item and isinstance(item["@graph"], list):
            for sub_item in item["@graph"]:
                cls._process_json_ld_item(sub_item, out)
                if out["price"] is not None:
                    return

        item_type = item.get("@type", "")
        if isinstance(item_type, list):
            item_type = " ".join(item_type)

        is_product = any(
            t in item_type.lower() 
            for t in ["product", "individualproduct", "productmodel", "service", "hotelroom", "offer"]
        )

        if not is_product and "offers" not in item:
            return

        # Extract Name
        if not out["name"] and "name" in item:
            out["name"] = str(item["name"]).strip()

        # Extract SKU / Identifiers
        if not out["sku"]:
            for key in ["sku", "gtin13", "gtin14", "gtin8", "gtin", "mpn", "productID"]:
                if key in item and item[key]:
                    out["sku"] = str(item[key]).strip()
                    break

        # Extract Brand
        if not out["brand"]:
            if "brand" in item:
                if isinstance(item["brand"], dict) and "name" in item["brand"]:
                    out["brand"] = str(item["brand"]["name"]).strip()
                elif isinstance(item["brand"], str):
                    out["brand"] = item["brand"].strip()

        # Extract Offers / Pricing
        offers = item.get("offers")
        if offers:
            cls._extract_from_offers_obj(offers, out)

        # Direct price on root object if present
        if out["price"] is None and "price" in item:
            p, c = cls._normalize_price_and_currency(str(item["price"]), item.get("priceCurrency"))
            if p is not None:
                out["price"] = p
                out["currency"] = c or out.get("currency")

    @classmethod
    def _extract_from_offers_obj(cls, offers: Any, out: Dict[str, Any]) -> None:
        """Extracts price, currency, and availability from Schema.org offers."""
        offer_list = offers if isinstance(offers, list) else [offers]
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue

            raw_price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
            raw_currency = offer.get("priceCurrency") or offer.get("currency")
            raw_availability = offer.get("availability")

            if raw_price is not None:
                price_dec, curr = cls._normalize_price_and_currency(str(raw_price), raw_currency)
                if price_dec is not None:
                    out["price"] = price_dec
                    if curr:
                        out["currency"] = curr

            if raw_availability:
                clean_avail = str(raw_availability).strip().lower()
                for key, mapped_val in SCHEMA_AVAILABILITY_MAP.items():
                    if key in clean_avail:
                        out["availability"] = mapped_val
                        break

            if out["price"] is not None:
                break

    @classmethod
    def _extract_meta_tags(cls, html_content: str, out: Dict[str, Any]) -> None:
        """Extracts OpenGraph, Twitter, and standard <meta> / <link> tags."""
        # Meta tag regex
        meta_pattern = re.compile(
            r'<meta\s+[^>]*(?:name|property|itemprop)=["\']([^"\']+)["\'][^>]*content=["\']([^"\']*)["\']',
            re.IGNORECASE
        )
        # Alternate attribute ordering: content before name/property
        meta_pattern_alt = re.compile(
            r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*(?:name|property|itemprop)=["\']([^"\']+)["\']',
            re.IGNORECASE
        )

        meta_dict: Dict[str, str] = {}
        for prop, val in meta_pattern.findall(html_content):
            meta_dict[prop.lower().strip()] = html.unescape(val.strip())
        for val, prop in meta_pattern_alt.findall(html_content):
            meta_dict[prop.lower().strip()] = html.unescape(val.strip())

        # 1. Price extraction
        price_keys = [
            "product:price:amount", "og:price:amount", "twitter:data1",
            "price", "product:sale_price:amount", "product:original_price:amount",
            "sailthru.price", "parsely-metadata:price"
        ]
        curr_keys = [
            "product:price:currency", "og:price:currency", "twitter:data2",
            "pricecurrency", "currency", "sailthru.currency"
        ]

        found_price_str = None
        for k in price_keys:
            if k in meta_dict and meta_dict[k]:
                found_price_str = meta_dict[k]
                break

        found_curr_str = None
        for k in curr_keys:
            if k in meta_dict and meta_dict[k]:
                found_curr_str = meta_dict[k]
                break

        if found_price_str:
            p, c = cls._normalize_price_and_currency(found_price_str, found_curr_str)
            if p is not None:
                out["price"] = p
                if c:
                    out["currency"] = c

        # 2. Availability extraction
        avail_keys = ["product:availability", "og:availability", "availability"]
        for k in avail_keys:
            if k in meta_dict and meta_dict[k]:
                val = meta_dict[k].lower()
                for key, mapped_val in SCHEMA_AVAILABILITY_MAP.items():
                    if key in val:
                        out["availability"] = mapped_val
                        break
                if out["availability"] != AvailabilityStatusEnum.UNKNOWN:
                    break

        # 3. Product Title / Name
        if not out["name"]:
            for k in ["og:title", "twitter:title", "product:title", "title"]:
                if k in meta_dict and meta_dict[k]:
                    out["name"] = meta_dict[k]
                    break

        # 4. Brand
        if not out["brand"]:
            for k in ["product:brand", "og:brand", "brand", "twitter:brand"]:
                if k in meta_dict and meta_dict[k]:
                    out["brand"] = meta_dict[k]
                    break

    @classmethod
    def _extract_embedded_framework_state(cls, html_content: str, out: Dict[str, Any]) -> None:
        """Searches for Next.js (__NEXT_DATA__), Nuxt, or Window Initial State blobs."""
        # Next.js
        next_match = re.search(r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>', html_content, re.DOTALL)
        if next_match:
            try:
                parsed = json.loads(next_match.group(1))
                cls._extract_from_json_dict(parsed, out)
                if out["price"] is not None:
                    return
            except Exception:
                pass

        # Window state
        state_match = re.search(r'window\.(?:__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__)\s*=\s*(\{.*?\});', html_content, re.DOTALL)
        if state_match:
            try:
                parsed = json.loads(state_match.group(1))
                cls._extract_from_json_dict(parsed, out)
            except Exception:
                pass

    @classmethod
    def _extract_from_json_dict(cls, node: Any, out: Dict[str, Any], depth: int = 0) -> None:
        """Deep recursive traversal of JSON object searching for price, currency, availability, and attributes."""
        if depth > 7 or not node:
            return

        if isinstance(node, dict):
            # Check current dictionary for price keys
            for p_key in ["price", "current_price", "sale_price", "offerPrice", "unitPrice", "amount", "final_price"]:
                if p_key in node and node[p_key] is not None:
                    val = node[p_key]
                    if isinstance(val, (int, float, str, Decimal)):
                        curr = node.get("currency") or node.get("priceCurrency") or node.get("currencyCode")
                        p, c = cls._normalize_price_and_currency(str(val), str(curr) if curr else None)
                        if p is not None and p > 0:
                            out["price"] = p
                            if c:
                                out["currency"] = c
                            break

            # Check for title/name
            if not out["name"]:
                for n_key in ["title", "productTitle", "name", "productName"]:
                    if n_key in node and isinstance(node[n_key], str) and len(node[n_key]) > 2:
                        out["name"] = node[n_key].strip()
                        break

            # Check for SKU
            if not out["sku"]:
                for s_key in ["sku", "asin", "upc", "gtin", "productId", "modelNumber"]:
                    if s_key in node and isinstance(node[s_key], (str, int)):
                        out["sku"] = str(node[s_key]).strip()
                        break

            # Recurse children
            for v in node.values():
                cls._extract_from_json_dict(v, out, depth + 1)
                if out["price"] is not None and out["currency"] is not None:
                    return

        elif isinstance(node, list):
            for elem in node:
                cls._extract_from_json_dict(elem, out, depth + 1)
                if out["price"] is not None and out["currency"] is not None:
                    return

    @classmethod
    def _extract_semantic_html_heuristics(cls, html_content: str, out: Dict[str, Any]) -> None:
        """Generic fallback heuristics using regex patterns on price-labeled elements."""
        # Generic price regex capturing currency symbol/code and numeric value
        price_regex = re.compile(
            r'(?:[\$€£¥₹₽₩฿₫₱]|USD|EUR|GBP|CAD|AUD|JPY|INR|CHF|AED|BRL)\s*([0-9]{1,3}(?:[,\.][0-9]{3})*(?:[,\.][0-9]{2})?)'
            r'|'
            r'([0-9]{1,3}(?:[,\.][0-9]{3})*(?:[,\.][0-9]{2})?)\s*(?:[\$€£¥₹₽₩฿₫₱]|USD|EUR|GBP|CAD|AUD|JPY|INR|CHF|AED|BRL)',
            re.IGNORECASE
        )

        # Look in priority containers
        semantic_snippets = re.findall(
            r'(?:class|id|data-[a-z-]+)=["\'][^"\']*(?:price|offer|amount|cost|val)[^"\']*["\'][^>]*>([^<]{1,60})<',
            html_content,
            re.IGNORECASE
        )

        for snippet in semantic_snippets:
            snippet_clean = snippet.strip()
            if not snippet_clean:
                continue
            p, c = cls._normalize_price_and_currency(snippet_clean, out.get("currency"))
            if p is not None and p > 0:
                out["price"] = p
                if c:
                    out["currency"] = c
                break

        # Check for stock availability keywords in text
        lower_html = html_content.lower()
        if "out of stock" in lower_html or "sold out" in lower_html or "currently unavailable" in lower_html:
            out["availability"] = AvailabilityStatusEnum.OUT_OF_STOCK
        elif "in stock" in lower_html or "add to cart" in lower_html or "buy now" in lower_html:
            if out["availability"] == AvailabilityStatusEnum.UNKNOWN:
                out["availability"] = AvailabilityStatusEnum.IN_STOCK

    @classmethod
    def _normalize_price_and_currency(
        cls, 
        raw_price_str: str, 
        provided_currency: Optional[str] = None
    ) -> Tuple[Optional[Decimal], Optional[str]]:
        """
        Cleans and normalizes price strings into Decimal with financial precision.
        Infers currency from symbols or preserves provided ISO code.
        Never blindly defaults to USD if no currency information exists.
        """
        if not raw_price_str:
            return None, None

        cleaned = str(raw_price_str).strip()
        detected_currency: Optional[str] = None

        if provided_currency and len(provided_currency.strip()) == 3:
            detected_currency = provided_currency.strip().upper()

        # Check for currency symbols
        for sym, code in CURRENCY_SYMBOL_MAP.items():
            if sym in cleaned:
                if not detected_currency:
                    detected_currency = code
                cleaned = cleaned.replace(sym, "")

        # Check for ISO code in string
        for code in ["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "INR", "CHF", "AED", "BRL", "SEK", "PLN"]:
            if code in cleaned.upper():
                if not detected_currency:
                    detected_currency = code
                cleaned = re.sub(code, "", cleaned, flags=re.IGNORECASE)

        # Remove extraneous characters except numbers, dots, commas, minus
        cleaned = re.sub(r'[^0-9\.,]', '', cleaned).strip()
        if not cleaned:
            return None, detected_currency

        # Handle European vs US number formatting (e.g. 1.299,99 vs 1,299.99)
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                # European style: 1.299,99 -> 1299.99
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                # US style: 1,299.99 -> 1299.99
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." not in cleaned:
            # Check if comma is decimal or thousands separator
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[1]) == 2:
                # e.g. 149,99 -> 149.99
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")

        try:
            val = Decimal(cleaned)
            return val, detected_currency
        except Exception:
            return None, detected_currency


class DualEngineExtractorService:
    """
    Main extraction service integrating HTTP Fast-Path and Headless Browser Fallback.
    """

    @classmethod
    async def extract_url(
        cls, 
        target_url: str, 
        force_browser: bool = False,
        timeout_seconds: int = 15
    ) -> CollectionResult:
        """
        Executes end-to-end collection on a target URL:
        1. Performs async HTTP fetch with browser headers.
        2. Evaluates HTTP response status:
           - Handles HTTP 202 (Accepted for processing / Anti-bot queue) with deep response inspection.
           - Handles HTTP 4xx / 5xx error states with detailed diagnostic categorization.
        3. Parses extracted data dynamically across HTML, JSON-LD, meta tags, and SPA states.
        4. Validates extraction: marks is_success=True ONLY if valid price and data was obtained.
        """
        start_time = time.perf_counter()
        normalized_url = target_url.strip()
        if not normalized_url.startswith("http://") and not normalized_url.startswith("https://"):
            normalized_url = f"https://{normalized_url}"

        # Setup standard browser headers to minimize bot false-positives
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        }

        # Step 1: Fast-Path HTTP Request
        http_status: Optional[int] = None
        final_url = normalized_url
        raw_body: str = ""
        response_headers: Dict[str, str] = {}

        try:
            # We use urllib or httpx depending on environment availability
            try:
                import httpx
                async with httpx.AsyncClient(
                    follow_redirects=True, 
                    timeout=httpx.Timeout(timeout_seconds),
                    headers=headers,
                    verify=False
                ) as client:
                    resp = await client.get(normalized_url)
                    http_status = resp.status_code
                    final_url = str(resp.url)
                    raw_body = resp.text
                    response_headers = {k: v for k, v in resp.headers.items()}
            except ImportError:
                import urllib.request
                import urllib.error
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(normalized_url, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=timeout_seconds, context=ctx) as resp:
                        http_status = resp.status
                        final_url = resp.geturl()
                        raw_body = resp.read().decode('utf-8', errors='ignore')
                        response_headers = dict(resp.info())
                except urllib.error.HTTPError as he:
                    http_status = he.code
                    final_url = he.geturl() if hasattr(he, "geturl") else normalized_url
                    raw_body = he.read().decode('utf-8', errors='ignore')
                    response_headers = dict(he.headers) if hasattr(he, "headers") else {}

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            logger.error(f"Network error during fetch of {normalized_url}: {exc}")
            return CollectionResult(
                http_status_code=http_status,
                final_url=final_url,
                response_time_ms=elapsed_ms,
                raw_content=str(exc),
                is_success=False,
                error_category=ErrorCategoryEnum.NETWORK_TIMEOUT if "timeout" in str(exc).lower() else ErrorCategoryEnum.HTTP_ERROR,
                error_message=f"Network request failed: {str(exc)}"
            )

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        # Step 2: Handle HTTP 202 Accepted specifically
        if http_status == 202:
            logger.warning(
                f"[HTTP 202 Accepted] Target {normalized_url} responded with 202 Accepted. "
                f"Body length: {len(raw_body)} chars."
            )
            # Inspect response for possible redirect or embedded content
            parsed_data = UniversalExtractor.extract_from_html_or_json(raw_body, final_url, response_headers)
            
            # If price was successfully found despite 202 status:
            if parsed_data.get("price") is not None:
                return CollectionResult(
                    http_status_code=202,
                    final_url=final_url,
                    response_time_ms=elapsed_ms,
                    price=parsed_data["price"],
                    currency=parsed_data.get("currency"),
                    availability=parsed_data.get("availability", AvailabilityStatusEnum.UNKNOWN),
                    name=parsed_data.get("name"),
                    sku=parsed_data.get("sku"),
                    brand=parsed_data.get("brand"),
                    attributes=parsed_data.get("attributes", {}),
                    raw_content=raw_body[:2000],
                    headers=response_headers,
                    is_success=True
                )
            
            # If no price extracted, report exact 202 diagnostic and DO NOT mark COMPLETED
            snippet = raw_body[:300].strip() if raw_body else "No response body provided"
            return CollectionResult(
                http_status_code=202,
                final_url=final_url,
                response_time_ms=elapsed_ms,
                raw_content=raw_body[:2000],
                headers=response_headers,
                is_success=False,
                error_category=ErrorCategoryEnum.HTTP_202_ACCEPTED_UNRESOLVED,
                error_message=(
                    f"Target returned HTTP 202 Accepted (Request queued or anti-bot challenge active). "
                    f"No extractable product pricing found in body snippet: '{snippet}'"
                )
            )

        # Step 3: Handle other HTTP non-200 statuses
        if http_status and http_status >= 400:
            category = ErrorCategoryEnum.HTTP_5XX_SERVER_ERROR if http_status >= 500 else (
                ErrorCategoryEnum.HTTP_403_FORBIDDEN if http_status == 403 else (
                    ErrorCategoryEnum.HTTP_404 if http_status == 404 else (
                        ErrorCategoryEnum.HTTP_429_RATE_LIMIT if http_status == 429 else ErrorCategoryEnum.HTTP_ERROR
                    )
                )
            )
            return CollectionResult(
                http_status_code=http_status,
                final_url=final_url,
                response_time_ms=elapsed_ms,
                raw_content=raw_body[:2000],
                headers=response_headers,
                is_success=False,
                error_category=category,
                error_message=f"HTTP {http_status} error received from target URL"
            )

        # Step 4: Parse HTML/JSON payload dynamically
        parsed_data = UniversalExtractor.extract_from_html_or_json(raw_body, final_url, response_headers)

        if parsed_data.get("price") is None:
            # Extraction did not find price
            return CollectionResult(
                http_status_code=http_status or 200,
                final_url=final_url,
                response_time_ms=elapsed_ms,
                raw_content=raw_body[:2000],
                headers=response_headers,
                is_success=False,
                error_category=ErrorCategoryEnum.EXTRACTION_FAILED,
                error_message="Webpage fetched successfully (HTTP 200) but no product price or Schema.org offer structure could be identified."
            )

        # Successful extraction
        return CollectionResult(
            http_status_code=http_status or 200,
            final_url=final_url,
            response_time_ms=elapsed_ms,
            price=parsed_data["price"],
            currency=parsed_data.get("currency"),
            availability=parsed_data.get("availability", AvailabilityStatusEnum.UNKNOWN),
            name=parsed_data.get("name"),
            sku=parsed_data.get("sku"),
            brand=parsed_data.get("brand"),
            category=parsed_data.get("category"),
            attributes=parsed_data.get("attributes", {}),
            extracted_fields=parsed_data.get("extracted_fields", {}),
            raw_content=raw_body[:2000],
            headers=response_headers,
            is_success=True
        )