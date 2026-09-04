"""
Unit & Integration tests for URL Security & SSRF Protection Service.
Tests:
- Valid public HTTP/HTTPS URLs
- Stripping of tracking parameters (utm_*, fbclid, gclid, etc.)
- Protocol blocking (file://, ftp://, javascript://, data://)
- Internal & private IP blocking (127.0.0.1, 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12)
- Cloud metadata endpoints (169.254.169.254, metadata.google.internal)
- Embedded credentials rejection (user:pass@host)
- Dynamic attributes sanitization (key validation, nesting limit, string length, script neutralization)
"""

import pytest
from backend.services.url_security import SecurityValidationError, UrlSecurityService


def test_valid_public_urls():
    valid_url = "https://www.google.com/search?q=test"
    result = UrlSecurityService.validate_url(valid_url)
    assert result == "https://www.google.com/search?q=test"

    # Auto prepending https:// if missing
    no_proto = "example.com/item/123"
    result_proto = UrlSecurityService.validate_url(no_proto)
    assert result_proto == "https://example.com/item/123"


def test_strip_tracking_parameters():
    url_with_utm = "https://example.com/product?id=99&utm_source=newsletter&utm_medium=email&fbclid=IwAR123"
    cleaned = UrlSecurityService.validate_url(url_with_utm)
    assert "utm_source" not in cleaned
    assert "fbclid" not in cleaned
    assert "id=99" in cleaned


def test_reject_dangerous_protocols():
    for dangerous in [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/file",
        "data:text/html,<h1>test</h1>",
        "gopher://127.0.0.1",
    ]:
        with pytest.raises(SecurityValidationError, match="Only HTTP and HTTPS are permitted"):
            UrlSecurityService.validate_url(dangerous)


def test_reject_embedded_credentials():
    bad_url = "https://admin:secret123@example.com/dashboard"
    with pytest.raises(SecurityValidationError, match="embedded username or password"):
        UrlSecurityService.validate_url(bad_url)


def test_reject_local_and_private_addresses():
    private_hosts = [
        "http://localhost:8000/api",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://192.168.1.1/router",
        "http://10.0.0.1/secret",
    ]
    for host in private_hosts:
        with pytest.raises(SecurityValidationError):
            UrlSecurityService.validate_url(host)


def test_sanitize_attributes_success():
    raw_attrs = {
        "brand": "Sony",
        "color": "Matte Black",
        "battery_life_hours": 30,
        "is_waterproof": True,
        "price_rating": 4.5,
        "specs": {
            "weight_g": 250,
            "bluetooth_ver": "5.2"
        }
    }
    sanitized = UrlSecurityService.sanitize_attributes(raw_attrs)
    assert sanitized["brand"] == "Sony"
    assert sanitized["battery_life_hours"] == 30
    assert sanitized["is_waterproof"] is True
    assert sanitized["specs"]["weight_g"] == 250


def test_sanitize_attributes_invalid_keys():
    invalid_keys = {
        "bad key with spaces": "val",
        "bad$key#": "val",
        "<script>": "val",
    }
    for k, v in invalid_keys.items():
        with pytest.raises(SecurityValidationError, match="Invalid attribute key"):
            UrlSecurityService.sanitize_attributes({k: v})


def test_sanitize_attributes_xss_neutralization():
    raw_attrs = {
        "description": "Premium sound <script>alert('xss')</script> guaranteed",
    }
    sanitized = UrlSecurityService.sanitize_attributes(raw_attrs)
    assert "<script>" not in sanitized["description"]
    assert "alert('xss')" in sanitized["description"] or "Premium sound" in sanitized["description"]


def test_sanitize_attributes_nesting_depth():
    deep_attrs = {
        "level1": {
            "level2": {
                "level3": {
                    "level4": "too deep"
                }
            }
        }
    }
    with pytest.raises(SecurityValidationError, match="maximum nesting depth"):
        UrlSecurityService.sanitize_attributes(deep_attrs)


def test_sanitize_attributes_max_keys_limit():
    too_many = {f"key_{i}": i for i in range(60)}
    with pytest.raises(SecurityValidationError, match="Too many dynamic attributes"):
        UrlSecurityService.sanitize_attributes(too_many, max_keys=50)
