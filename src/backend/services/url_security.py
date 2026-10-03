"""
URL Security & SSRF Protection Service.
Validates inbound URLs to prevent Server-Side Request Forgery (SSRF),
internal network probing, link-local / cloud metadata attacks, and payload bombing.
"""

import ipaddress
import re
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Private, Loopback, Link-Local, and Cloud Metadata Networks to strictly block
BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),          # Current network
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918 Private Class A
    ipaddress.ip_network("127.0.0.0/8"),        # Loopback
    ipaddress.ip_network("169.254.0.0/16"),     # Link-Local / Cloud Metadata (169.254.169.254)
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918 Private Class B
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918 Private Class C
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved
    ipaddress.ip_network("::1/128"),            # IPv6 Loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 Unique Local Address
    ipaddress.ip_network("fe80::/10"),          # IPv6 Link-Local
    ipaddress.ip_network("100.64.0.0/10"),      # Carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking (RFC 2544)
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("64:ff9b::/96"),       # NAT64 (can embed private IPv4)
]

# Tracking query parameters to strip during canonicalization
TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "msclkid", "mc_eid", "yclid", "_ga", "_gl")

# Attribute key name safety: alphanumeric with underscores or dashes
ATTR_KEY_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Suspicious / malicious HTML script tag patterns for stored XSS prevention
HTML_SCRIPT_PATTERN = re.compile(r"<[^>]*script[^>]*>|javascript:|data:text/html", re.IGNORECASE)


class SecurityValidationError(ValueError):
    """Raised when URL or attribute fails security/SSRF validation."""
    pass


class UrlSecurityService:
    """
    Validates target URLs against SSRF, internal network scanning, and malformed inputs.
    """

    @staticmethod
    def is_ip_blocked(ip_str: str) -> bool:
        """True if the address must never be fetched."""
        ip_obj = ipaddress.ip_address(ip_str.split("%", 1)[0])
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
            ip_obj = ip_obj.ipv4_mapped
        if not ip_obj.is_global:
            return True
        return any(ip_obj in net for net in BLOCKED_IP_NETWORKS)

    @classmethod
    def validate_url(cls, url: Optional[str], allow_empty: bool = True) -> Optional[str]:
        """
        Validates that a URL is safe to persist and fetch:
        1. Must use http or https scheme. Rejects file, ftp, gopher, data, javascript, etc.
        2. Must NOT contain embedded credentials (user:pass@host).
        3. Must have a valid, resolvable hostname.
        4. Resolved IP must NOT belong to loopback, private, link-local, or cloud metadata ranges.
        5. Canonicalizes and strips tracking parameters (utm_*, fbclid, etc.).
        """
        if not url or not url.strip():
            if allow_empty:
                return None
            raise SecurityValidationError("URL cannot be empty.")

        clean_url = url.strip()

        # Reject explicitly dangerous non-HTTP URI schemes
        lowered_raw = clean_url.lower()
        if lowered_raw.startswith(("file:", "ftp:", "gopher:", "data:", "javascript:", "blob:", "about:", "ws:", "wss:")):
            raise SecurityValidationError("Invalid URL protocol. Only HTTP and HTTPS are permitted.")

        if not clean_url.startswith(("http://", "https://")):
            clean_url = f"https://{clean_url}"

        parsed = urlparse(clean_url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise SecurityValidationError(
                f"Invalid URL protocol '{parsed.scheme}'. Only HTTP and HTTPS are permitted."
            )

        # Reject URLs containing username or password credentials
        if parsed.username or parsed.password:
            raise SecurityValidationError("URLs containing embedded username or password credentials are not permitted.")

        hostname = parsed.hostname
        if not hostname:
            raise SecurityValidationError("Invalid URL: Hostname is missing or malformed.")

        # Disallow explicit localhost or metadata hostnames
        lowered_host = hostname.lower()
        if lowered_host in (
            "localhost",
            "127.0.0.1",
            "::1",
            "metadata.google.internal",
            "169.254.169.254",
            "instance-data",
        ):
            raise SecurityValidationError("Access to local or cloud internal hosts is strictly forbidden.")

        # Resolve hostname to verify against blocked CIDR blocks
        try:
            addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            for family, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                if cls.is_ip_blocked(ip_str):
                    raise SecurityValidationError(
                        f"Target host resolves to a restricted private or link-local address ({ip_str})."
                    )
        except socket.gaierror:
            # If domain has valid TLD and syntax, check for suspicious internal suffixes
            if "." not in hostname or hostname.endswith((".local", ".internal", ".corp", ".lan", ".localhost")):
                raise SecurityValidationError(f"Domain '{hostname}' is not a valid or resolvable public domain.")

        # Canonicalize URL & strip tracking query parameters
        clean_query = cls._strip_tracking_params(parsed.query)
        canonical_parsed = parsed._replace(query=clean_query, fragment="")
        return urlunparse(canonical_parsed)

    @classmethod
    def _strip_tracking_params(cls, query_string: str) -> str:
        """Removes tracking query parameters like utm_source, fbclid, etc."""
        if not query_string:
            return ""
        pairs = parse_qsl(query_string, keep_blank_values=True)
        filtered_pairs = [
            (k, v) for k, v in pairs
            if not any(k.lower().startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)
        ]
        return urlencode(filtered_pairs)

    @classmethod
    def sanitize_attributes(
        cls,
        attributes: Dict[str, Any],
        max_keys: int = 50,
        max_str_len: int = 500
    ) -> Dict[str, Any]:
        """
        Sanitizes dynamic JSONB attributes:
        1. Enforces maximum key limit (max 50, prevents JSON bombing).
        2. Validates key names against safe regex (^[a-zA-Z0-9_-]{1,64}$).
        3. Strips script tags/HTML injections from strings and caps length (500 chars).
        4. Rejects deeply nested dicts (> 2 levels).
        """
        if not isinstance(attributes, dict):
            return {}

        if len(attributes) > max_keys:
            raise SecurityValidationError(
                f"Too many dynamic attributes ({len(attributes)}). Maximum allowed is {max_keys}."
            )

        sanitized: Dict[str, Any] = {}

        for key, val in attributes.items():
            clean_key = str(key).strip()
            if not ATTR_KEY_REGEX.match(clean_key):
                raise SecurityValidationError(
                    f"Invalid attribute key '{clean_key}'. Keys must be alphanumeric (a-z, 0-9, _, -) and <= 64 chars."
                )

            sanitized[clean_key] = cls._sanitize_value(val, max_str_len, depth=0)

        return sanitized

    @classmethod
    def _sanitize_value(cls, val: Any, max_str_len: int, depth: int) -> Any:
        if depth > 2:
            raise SecurityValidationError("Dynamic attributes exceed maximum nesting depth of 2.")

        if val is None:
            return None
        elif isinstance(val, (int, float, bool)):
            return val
        elif isinstance(val, str):
            clean_str = val.strip()
            if HTML_SCRIPT_PATTERN.search(clean_str):
                clean_str = HTML_SCRIPT_PATTERN.sub("", clean_str)
            if len(clean_str) > max_str_len:
                clean_str = clean_str[:max_str_len]
            return clean_str
        elif isinstance(val, list):
            return [cls._sanitize_value(item, max_str_len, depth + 1) for item in val[:50]]
        elif isinstance(val, dict):
            return {
                str(k)[:64]: cls._sanitize_value(v, max_str_len, depth + 1)
                for k, v in list(val.items())[:50]
            }
        else:
            return str(val)[:max_str_len]
