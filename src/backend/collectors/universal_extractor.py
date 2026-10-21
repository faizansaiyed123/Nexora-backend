"""
UniversalExtractor - Production-ready extraction for any e-commerce site.

Pipeline:
  URL -> HTML -> [Strategy 1: Site-specific selectors]
              -> [Strategy 2: JSON-LD / Schema.org]
              -> [Strategy 3: SPA embedded state (__NEXT_DATA__, __NUXT__, etc.)]
              -> [Strategy 4: Meta / OpenGraph]
              -> [Strategy 5: Embedded JSON scripts]
              -> [Strategy 6: Microdata / RDFa]
              -> [Strategy 7: Generic HTML selectors]
              -> [Strategy 8: Text pattern detection]
              -> Result (price, currency, availability, name, sku, brand, etc.)

No site is hardcoded beyond selector presets. Custom selectors per source
always take precedence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = __import__("logging").getLogger("nexora.universal_extractor")




@dataclass
class ExtractionResult:
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    name: Optional[str] = None
    sku: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    image: Optional[str] = None
    description: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    strategy_used: str = "NONE"
    confidence: float = 0.0




def _parse_price(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if text.count("-") > 1:
        return None
    if "-" in text and not text.startswith("-"):
        text = text.replace("-", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[-1]) in (1, 2):
            text = parts[0] + "." + parts[1]
        else:
            text = text.replace(",", "")
    try:
        price = Decimal(text)
        if price < 0:
            return None
        return price
    except InvalidOperation:
        return None


_SYMBOL_MAP = {
    "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY",
    "C$": "CAD", "A$": "AUD", "CA$": "CAD", "AU$": "AUD",
}
_ISO_CURRENCIES = {
    "USD", "EUR", "GBP", "INR", "JPY", "CAD", "AUD", "CHF", "CNY", "SGD",
    "NZD", "AED", "SAR", "SEK", "NOK", "DKK", "HKD", "KRW", "BRL", "MXN",
    "ZAR", "TRY", "RUB", "PLN", "THB",
}


def _normalize_currency(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in _SYMBOL_MAP:
        return _SYMBOL_MAP[text]
    match = re.search(r"\b[A-Za-z]{3}\b", text)
    if match:
        code = match.group(0).upper()
        if code in _ISO_CURRENCIES:
            return code
    return None


def _normalize_availability(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).lower().strip()
    if not text:
        return None
    if "discontinued" in text or "no longer available" in text:
        return "DISCONTINUED"
    if "backorder" in text or "back order" in text or "back-ordered" in text:
        return "BACKORDER"
    if "preorder" in text or "pre-order" in text or "pre order" in text:
        return "PREORDER"
    if (
        "outofstock" in text
        or "out of stock" in text
        or "out-of-stock" in text
        or "sold out" in text
        or "unavailable" in text
    ):
        return "OUT_OF_STOCK"
    if "instock" in text or "in stock" in text or "in-stock" in text or text == "available":
        return "IN_STOCK"
    return None




SITE_PRESETS: dict[str, dict[str, Any]] = {
    "amazon.in": {
        "price_selector": "#corePriceDisplay_desktop_section_div .a-price-whole, #priceblock_ourprice, #priceblock_dealprice, .a-price .a-offscreen",
        "currency_selector": "#corePriceDisplay_desktop_section_div .a-price-symbol, .a-price .a-price-symbol",
        "availability_selector": "#availability span, #availability .a-color-state",
        "name_selector": "#productTitle",
        "sku_selector": "#ASIN, [data-asin]",
        "brand_selector": "#bylineInfo, .a-link-normal#brand",
        "category_selector": "#wayfinding-breadcrumbs_container .a-list-item",
        "image_selector": "#landingImage, #imgBlkFront",
        "description_selector": "#feature-bullets li, #productDescription p",
    },
    "amazon.com": {
        "price_selector": "#corePriceDisplay_desktop_section_div .a-price-whole, #priceblock_ourprice, #priceblock_dealprice, .a-price .a-offscreen",
        "currency_selector": "#corePriceDisplay_desktop_section_div .a-price-symbol, .a-price .a-price-symbol",
        "availability_selector": "#availability span, #availability .a-color-state",
        "name_selector": "#productTitle",
        "sku_selector": "#ASIN, [data-asin]",
        "brand_selector": "#bylineInfo, .a-link-normal#brand",
        "category_selector": "#wayfinding-breadcrumbs_container .a-list-item",
        "image_selector": "#landingImage, #imgBlkFront",
        "description_selector": "#feature-bullets li, #productDescription p",
    },
    "amazon.co.uk": {
        "price_selector": "#corePriceDisplay_desktop_section_div .a-price-whole, #priceblock_ourprice, #priceblock_dealprice, .a-price .a-offscreen",
        "currency_selector": "#corePriceDisplay_desktop_section_div .a-price-symbol, .a-price .a-price-symbol",
        "availability_selector": "#availability span, #availability .a-color-state",
        "name_selector": "#productTitle",
        "sku_selector": "#ASIN, [data-asin]",
        "brand_selector": "#bylineInfo, .a-link-normal#brand",
        "category_selector": "#wayfinding-breadcrumbs_container .a-list-item",
        "image_selector": "#landingImage, #imgBlkFront",
        "description_selector": "#feature-bullets li, #productDescription p",
    },
    "flipkart.com": {
        "price_selector": "div.Nx9bqj, ._30jeq3._16Jk6d, ._1_WHN1, .CEpuCY",
        "currency_selector": "div.Nx9bqj, ._30jeq3._16Jk6d",
        "availability_selector": ".MJ4y8M, ._16FRp0",
        "name_selector": "span.VU-GEz, span.B_NuCI, .yhB1nd",
        "sku_selector": ".m1n9Z0, ._1J58kU",
        "brand_selector": "span.G6XhB_U, ._2B_pmu",
        "category_selector": ".s1P8Mq a, ._2tJFpN a",
        "image_selector": ".DByuf4 img, ._396cs4 img, .CXW8mj img",
        "description_selector": "._1mXcCf.Rmojen, ._1mXcCf",
    },
    "in.puma.com": {
        "price_selector": ".puma-price, .product-price, [data-testid='product-price'], .price-current",
        "currency_selector": ".puma-price, .product-price",
        "availability_selector": ".product-availability, .stock-status, [data-testid='availability']",
        "name_selector": "h1.product-title, .product-name, h1[data-testid='product-title']",
        "sku_selector": ".product-sku, [data-testid='sku'], .product-code",
        "brand_selector": ".brand-name, .product-brand",
        "category_selector": ".breadcrumb li a, .category-path",
        "image_selector": ".product-image img, .gallery-image img, [data-testid='product-image']",
        "description_selector": ".product-description, .product-details p",
    },
    "www.makemytrip.com": {
        "price_selector": ".widget__price, .price__amount, .actual-price, [data-cy='price']",
        "currency_selector": ".widget__price, .price__currency",
        "availability_selector": ".availability, .status, .seat-availability",
        "name_selector": ".hotelName, .packageName, h1",
        "sku_selector": ".propertyCode, .packageCode",
        "brand_selector": ".brand, .provider",
        "category_selector": ".breadcrumb a",
        "image_selector": ".imageContainer img, .photo img",
        "description_selector": ".description p, .overview",
    },
    "www.goibibo.com": {
        "price_selector": ".price__amount, .actualPrice, [data-cy='price']",
        "currency_selector": ".price__currency",
        "availability_selector": ".availability, .status",
        "name_selector": ".propertyName, h1",
        "sku_selector": ".propertyId",
        "brand_selector": ".provider",
        "category_selector": ".breadcrumb a",
        "image_selector": ".image img",
        "description_selector": ".description",
    },
    "__default__": {
        "price_selector": '[itemprop="price"], [data-price], [data-product-price], [data-sale-price], [data-current-price], [class*="price"], [id*="price"], .price, .product-price, .sale-price, .current-price',
        "currency_selector": '[itemprop="priceCurrency"], [data-currency], .currency',
        "availability_selector": '[itemprop="availability"], [class*="availability"], [id*="availability"], [class*="stock"], [id*="stock"]',
        "name_selector": "h1, [itemprop=" + '"name"' + "], [data-product-name], .product-title, .product-name",
        "sku_selector": '[itemprop="sku"], [data-sku], [data-product-sku], .sku, .product-sku',
        "brand_selector": '[itemprop="brand"], [data-brand], .brand, .product-brand',
        "category_selector": '[itemprop="category"], .breadcrumb a, .category-path, .category',
        "image_selector": '[itemprop="image"], .product-image img, .gallery-image img, .product-img img',
        "description_selector": '[itemprop="description"], .product-description, .product-details p, .description',
    },
}


def get_site_preset(url: str) -> dict[str, Any]:
    """Return selector preset for the given URL, falling back to default."""
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        if host in SITE_PRESETS:
            return SITE_PRESETS[host]
        for key in SITE_PRESETS:
            if key != "__default__" and host.endswith(key):
                return SITE_PRESETS[key]
    except Exception:
        pass
    return dict(SITE_PRESETS["__default__"])




def _get_text(element: Any) -> str:
    if element is None:
        return ""
    if hasattr(element, "get_text"):
        return element.get_text(" ", strip=True)
    return str(element or "").strip()


def _get_attr(element: Any, *names: str) -> Optional[str]:
    if element is None:
        return None
    for name in names:
        try:
            value = element.get(name)
        except Exception:
            continue
        if value:
            return str(value).strip()
    return None


def _resolve_url(url: str, base_url: str) -> Optional[str]:
    try:
        return urljoin(base_url, url)
    except Exception:
        return url




def _extract_with_custom_selectors(
    soup: BeautifulSoup,
    base_url: str,
    custom_selectors: Optional[dict[str, Any]],
) -> ExtractionResult:
    if not isinstance(custom_selectors, dict) or not custom_selectors:
        return ExtractionResult()

    result = ExtractionResult()
    result.strategy_used = "CUSTOM_SELECTORS"
    result.confidence = 0.98

    price_sel = custom_selectors.get("price_selector")
    currency_sel = custom_selectors.get("currency_selector")
    availability_sel = custom_selectors.get("availability_selector")
    name_sel = custom_selectors.get("name_selector")
    sku_sel = custom_selectors.get("sku_selector")
    brand_sel = custom_selectors.get("brand_selector")
    category_sel = custom_selectors.get("category_selector")
    image_sel = custom_selectors.get("image_selector")
    description_sel = custom_selectors.get("description_selector")

    if price_sel:
        for el in soup.select(str(price_sel)):
            val = _get_text(el)
            p = _parse_price(val)
            if p is not None and p > 0:
                result.price = p
                result.attributes["price_selector"] = str(price_sel)
                break

    if currency_sel:
        el = soup.select_one(str(currency_sel))
        if el:
            result.currency = _normalize_currency(_get_text(el))

    if availability_sel:
        el = soup.select_one(str(availability_sel))
        if el:
            result.availability = _normalize_availability(_get_text(el))

    if name_sel:
        el = soup.select_one(str(name_sel))
        if el:
            result.name = _get_text(el) or None

    if sku_sel:
        el = soup.select_one(str(sku_sel))
        if el:
            result.sku = _get_attr(el, "data-sku", "data-product-sku", "value") or _get_text(el)

    if brand_sel:
        el = soup.select_one(str(brand_sel))
        if el:
            result.brand = _get_text(el) or None

    if category_sel:
        el = soup.select_one(str(category_sel))
        if el:
            result.category = _get_text(el) or None

    if image_sel:
        el = soup.select_one(str(image_sel))
        if el:
            result.image = _resolve_url(_get_attr(el, "src", "data-src", "href", "content") or "", base_url)

    if description_sel:
        el = soup.select_one(str(description_sel))
        if el:
            result.description = _get_text(el) or None

    return result




def _extract_json_ld(soup: BeautifulSoup) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "JSON_LD"
    result.confidence = 0.90

    scripts = soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)})
    for script in scripts:
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            continue

        prod_objects = _find_product_objects(data)
        for obj in prod_objects:
            if not isinstance(obj, dict):
                continue

            if result.price is None:
                for key in ("price", "lowPrice", "highPrice"):
                    if key in obj:
                        p = _parse_price(obj.get(key))
                        if p is not None and p > 0:
                            result.price = p
                            break

            if result.currency is None:
                for key in ("priceCurrency", "currency", "currencyCode"):
                    if key in obj:
                        c = _normalize_currency(obj.get(key))
                        if c:
                            result.currency = c
                            break

            if result.availability is None:
                for key in ("availability", "availabilityStatus", "stock", "stockStatus"):
                    if key in obj:
                        a = _normalize_availability(obj.get(key))
                        if a:
                            result.availability = a
                            break

            if result.name is None and "name" in obj:
                result.name = str(obj.get("name")).strip() or None
            if result.sku is None and "sku" in obj:
                result.sku = str(obj.get("sku")).strip() or None
            if result.brand is None and "brand" in obj:
                brand = obj.get("brand")
                if isinstance(brand, dict):
                    result.brand = brand.get("name") or None
                else:
                    result.brand = str(brand).strip() or None
            if result.category is None and "category" in obj:
                result.category = str(obj.get("category")).strip() or None
            if result.image is None and "image" in obj:
                img = obj.get("image")
                if isinstance(img, str):
                    result.image = img
                elif isinstance(img, dict):
                    result.image = img.get("url")

            if result.price is not None:
                break

    return result




_SPA_PATTERNS = [
    re.compile(r"<script[^>]*id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", re.S | re.I),
    re.compile(r"<script[^>]*id=\"__NUXT_DATA__\"[^>]*>(.*?)</script>", re.S | re.I),
    re.compile(r"window\.__NUXT__\s*=\s*(.*?);</script>", re.S | re.I),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(.*?);</script>", re.S | re.I),
    re.compile(r"window\.__data__\s*=\s*(.*?);</script>", re.S | re.I),
    re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(.*?);</script>", re.S | re.I),
    re.compile(r"window\.Shopify\s*=\s*(.*?);</script>", re.S | re.I),
    re.compile(r"window\.product\s*=\s*(.*?);</script>", re.S | re.I),
    re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(.*?);</script>", re.S | re.I),
]


def _extract_spa_state(html: str, base_url: str) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "SPA_STATE"
    result.confidence = 0.85

    for pattern in _SPA_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        products = _find_product_objects(data)
        for prod in products:
            if not isinstance(prod, dict):
                continue

            if result.price is None:
                for key in ("price", "salePrice", "compareAtPrice", "currentPrice", "regularPrice", "offerPrice"):
                    if key in prod:
                        p = _parse_price(prod.get(key))
                        if p is not None and p > 0:
                            result.price = p
                            break

            if result.currency is None:
                for key in ("currency", "currencyCode", "priceCurrency"):
                    if key in prod:
                        c = _normalize_currency(prod.get(key))
                        if c:
                            result.currency = c
                            break

            if result.availability is None:
                for key in ("availability", "availabilityStatus", "stockStatus", "inStock"):
                    if key in prod:
                        a = _normalize_availability(prod.get(key))
                        if a:
                            result.availability = a
                            break

            if result.name is None and "name" in prod:
                result.name = str(prod.get("name")).strip() or None
            if result.sku is None and "sku" in prod:
                result.sku = str(prod.get("sku")).strip() or None
            if result.brand is None and "brand" in prod:
                result.brand = str(prod.get("brand")).strip() or None
            if result.category is None and "category" in prod:
                result.category = str(prod.get("category")).strip() or None
            if result.image is None and "image" in prod:
                img = prod.get("image")
                if isinstance(img, str):
                    result.image = img
                elif isinstance(img, dict):
                    result.image = img.get("url") or img.get("src")

            if result.price is not None:
                break

        if result.price is not None:
            break

    return result


def _find_product_objects(data: Any, depth: int = 0) -> list[dict]:
    """Recursively find objects that look like products."""
    if depth > 6:
        return []
    results: list[dict] = []

    if isinstance(data, dict):
        keys = set(data.keys())
        productish = any(
            k in keys for k in ("price", "name", "title", "sku", "skuCode", "variant", "variants")
        )
        if productish:
            results.append(data)
        for child in data.values():
            results.extend(_find_product_objects(child, depth + 1))

    elif isinstance(data, list):
        for child in data:
            results.extend(_find_product_objects(child, depth + 1))

    return results




def _extract_meta(soup: BeautifulSoup) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "META"
    result.confidence = 0.75

    price_fields = [
        "product:price:amount", "og:price:amount", "price",
        "product-price", "price:amount",
    ]
    currency_fields = [
        "product:price:currency", "og:price:currency",
        "priceCurrency", "currency",
    ]
    availability_fields = [
        "product:availability", "availability",
        "product:availability:status",
    ]
    name_fields = ["og:title", "product:name", "title", "og:product:name"]
    image_fields = ["og:image", "product:image", "og:image:url"]
    sku_fields = ["product:sku", "sku", "product-id"]

    for name in price_fields:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el:
            val = el.get("content")
            if val:
                p = _parse_price(val)
                if p is not None and p > 0:
                    result.price = p
                    break

    for name in currency_fields:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el:
            val = el.get("content")
            if val:
                result.currency = _normalize_currency(val)
                if result.currency:
                    break

    for name in availability_fields:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el:
            val = el.get("content")
            if val:
                result.availability = _normalize_availability(val)
                if result.availability:
                    break

    for name in name_fields:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el:
            val = el.get("content")
            if val:
                result.name = val.strip() or None
                break

    for name in image_fields:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el:
            val = el.get("content")
            if val:
                result.image = val.strip() or None
                break

    for name in sku_fields:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el:
            val = el.get("content")
            if val:
                result.sku = val.strip() or None
                break

    return result




def _extract_embedded_json(soup: BeautifulSoup) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "EMBEDDED_JSON"
    result.confidence = 0.80

    scripts = soup.find_all("script")
    for script in scripts:
        script_type = (script.get("type") or "").lower()
        if "json" not in script_type or "ld+json" in script_type:
            continue
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            continue

        prods = _find_product_objects(data)
        for prod in prods:
            if not isinstance(prod, dict):
                continue
            if result.price is None:
                for key in ("price", "salePrice", "currentPrice", "regularPrice"):
                    if key in prod:
                        p = _parse_price(prod.get(key))
                        if p is not None and p > 0:
                            result.price = p
                            break
            if result.currency is None and "currency" in prod:
                result.currency = _normalize_currency(prod.get("currency"))
            if result.name is None and "name" in prod:
                result.name = str(prod.get("name")).strip() or None
            if result.sku is None and "sku" in prod:
                result.sku = str(prod.get("sku")).strip() or None
            if result.price is not None:
                break

    return result




def _extract_microdata(soup: BeautifulSoup) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "MICRODATA"
    result.confidence = 0.70

    elements = soup.select("[itemprop]")
    for element in elements:
        key = element.get("itemprop")
        if not key:
            continue
        value = (
            element.get("content")
            or element.get("value")
            or element.get("href")
            or element.get_text(" ", strip=True)
        )
        if value is None:
            continue

        if key == "price" and result.price is None:
            p = _parse_price(value)
            if p is not None and p > 0:
                result.price = p
        elif key == "priceCurrency" and result.currency is None:
            result.currency = _normalize_currency(value)
        elif key == "availability" and result.availability is None:
            result.availability = _normalize_availability(value)
        elif key == "name" and result.name is None:
            result.name = value.strip() or None
        elif key == "sku" and result.sku is None:
            result.sku = value.strip() or None
        elif key == "brand" and result.brand is None:
            result.brand = value.strip() or None
        elif key == "category" and result.category is None:
            result.category = value.strip() or None

    return result




def _extract_html_selectors(soup: BeautifulSoup, base_url: str) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "HTML_SELECTORS"
    result.confidence = 0.60

    price_selectors = [
        '[itemprop="price"]',
        "[data-price]",
        "[data-product-price]",
        "[data-sale-price]",
        "[data-current-price]",
        '[class*="price"]',
        '[id*="price"]',
        ".price",
        ".product-price",
        ".sale-price",
        ".current-price",
        ".offer-price",
    ]
    for selector in price_selectors:
        for el in soup.select(selector):
            val = (
                el.get("content")
                or el.get("data-price")
                or el.get("data-product-price")
                or el.get("data-sale-price")
                or el.get("data-current-price")
                or el.get_text(" ", strip=True)
            )
            p = _parse_price(val)
            if p is not None and p > 0:
                result.price = p
                break
        if result.price is not None:
            break

    if result.currency is None:
        el = soup.select_one('[itemprop="priceCurrency"]')
        if el:
            val = el.get("content") or el.get_text(strip=True)
            result.currency = _normalize_currency(val)

    if result.availability is None:
        availability_selectors = [
            '[itemprop="availability"]',
            '[class*="availability"]',
            '[id*="availability"]',
            '[class*="stock"]',
            '[id*="stock"]',
        ]
        for selector in availability_selectors:
            for el in soup.select(selector):
                val = el.get("href") or el.get("content") or el.get_text(" ", strip=True)
                a = _normalize_availability(val)
                if a:
                    result.availability = a
                    break
            if result.availability:
                break

    if result.name is None:
        el = soup.find("h1") or soup.select_one('[itemprop="name"]')
        if el:
            result.name = el.get_text(" ", strip=True) or None

    if result.image is None:
        el = soup.select_one('[itemprop="image"], .product-image img, .gallery-image img')
        if el:
            result.image = _resolve_url(el.get("src") or el.get("data-src") or "", base_url)

    return result




def _extract_from_text(soup: BeautifulSoup, currency_hint: Optional[str] = None) -> ExtractionResult:
    result = ExtractionResult()
    result.strategy_used = "TEXT_DETECTION"
    result.confidence = 0.40

    text = soup.get_text(" ", strip=True)
    if not text:
        return result

    currency = currency_hint or _detect_currency_from_text(text)
    if currency:
        patterns = _get_price_patterns_for_currency(currency)
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                p = _parse_price(m.group(0))
                if p is not None and p > 0:
                    result.price = p
                    result.currency = currency
                    break

    if result.availability is None:
        result.availability = _normalize_availability(text)

    return result


def _detect_currency_from_text(text: str) -> Optional[str]:
    patterns = [
        (r"\bUSD\b", "USD"), (r"\bEUR\b", "EUR"), (r"\bGBP\b", "GBP"),
        (r"\bINR\b", "INR"), (r"\bJPY\b", "JPY"), (r"\bCAD\b", "CAD"),
        (r"\bAUD\b", "AUD"), (r"\bCHF\b", "CHF"), (r"\bCNY\b", "CNY"),
        (r"\bSGD\b", "SGD"), (r"\$", "USD"), (r"€", "EUR"),
        (r"£", "GBP"), (r"₹", "INR"), (r"¥", "JPY"),
    ]
    for pattern, currency in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return currency
    return None


def _get_price_patterns_for_currency(currency: str) -> list[str]:
    if currency == "USD":
        return [r"\$\s*\d[\d,]*(?:\.\d{1,4})?", r"\bUSD\s*\d[\d,]*(?:\.\d{1,4})?"]
    if currency == "EUR":
        return [r"€\s*\d[\d.,]*", r"\bEUR\s*\d[\d.,]*"]
    if currency == "GBP":
        return [r"£\s*\d[\d,]*(?:\.\d{1,4})?", r"\bGBP\s*\d[\d,]*(?:\.\d{1,4})?"]
    if currency == "INR":
        return [r"₹\s*\d[\d,]*(?:\.\d{1,4})?", r"\bINR\s*\d[\d,]*(?:\.\d{1,4})?"]
    if currency == "JPY":
        return [r"¥\s*\d[\d,]*", r"\bJPY\s*\d[\d,]*"]
    return [r"[\$€£₹¥]\s*\d[\d,]*(?:\.\d{1,4})?"]




class UniversalExtractor:
    """
    Production-ready extractor that tries multiple strategies in order,
    stopping as soon as a price is found.

    Usage:
        extractor = UniversalExtractor()
        result = extractor.extract(html, base_url, custom_selectors=site_selectors)
    """

    def __init__(self) -> None:
        self.strategies = [
            ("custom_selectors", self._run_custom_selectors),
            ("json_ld", self._run_json_ld),
            ("spa_state", self._run_spa_state),
            ("meta", self._run_meta),
            ("embedded_json", self._run_embedded_json),
            ("microdata", self._run_microdata),
            ("html_selectors", self._run_html_selectors),
            ("text_detection", self._run_text_detection),
        ]


    def extract(
        self,
        html: str,
        base_url: str,
        custom_selectors: Optional[dict[str, Any]] = None,
    ) -> ExtractionResult:
        """
        Extract product data from HTML using the full fallback chain.

        Returns the first strategy that produces a price, or the best
        partial result from all strategies.
        """
        soup = BeautifulSoup(html, "html.parser")

        best_result: Optional[ExtractionResult] = None
        best_confidence = 0.0

        for name, strategy in self.strategies:
            try:
                if name == "custom_selectors":
                    res = strategy(soup, base_url, custom_selectors)
                else:
                    res = strategy(soup, base_url, html)

                if res.confidence > best_confidence:
                    best_result = res
                    best_confidence = res.confidence

                if res.price is not None and res.confidence >= 0.85:
                    return res
            except Exception as exc:
                logger.debug("Extraction strategy %s failed: %s", name, exc)
                continue

        if best_result:
            return best_result

        return ExtractionResult(strategy_used="NONE", confidence=0.0)


    def _run_custom_selectors(
        self, soup: BeautifulSoup, base_url: str, custom_selectors: Optional[dict[str, Any]]
    ) -> ExtractionResult:
        return _extract_with_custom_selectors(soup, base_url, custom_selectors)

    def _run_json_ld(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_json_ld(soup)

    def _run_spa_state(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_spa_state(html, base_url)

    def _run_meta(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_meta(soup)

    def _run_embedded_json(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_embedded_json(soup)

    def _run_microdata(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_microdata(soup)

    def _run_html_selectors(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_html_selectors(soup, base_url)

    def _run_text_detection(
        self, soup: BeautifulSoup, base_url: str, html: str
    ) -> ExtractionResult:
        return _extract_from_text(soup)
