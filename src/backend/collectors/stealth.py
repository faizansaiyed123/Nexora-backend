"""Stealth patches applied to Playwright pages to reduce bot-detection surface.

These are legitimate fingerprint-spoothing techniques used by competitive-intelligence
tools that monitor public pricing. They do NOT solve CAPTCHAs, do NOT bypass
authentication, and do NOT defeat Cloudflare/Akamai enterprise bot management.
They only reduce the chance that a well-behaved crawler is flagged as automated.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

logger = logging.getLogger("nexora.stealth")

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_PLUGIN_DATA = [
    {"name": "PDF Viewer", "description": "Portable Document Format"},
    {"name": "Chrome PDF Viewer", "description": "Portable Document Format"},
    {"name": "Chromium PDF Viewer", "description": "Portable Document Format"},
    {"name": "Microsoft Edge PDF Viewer", "description": "Portable Document Format"},
    {"name": "WebKit built-in PDF", "description": "Portable Document Format"},
]

_MIME_TYPES = [
    "application/pdf",
    "text/pdf",
]


def random_user_agent() -> str:
    """Return a random desktop Chrome user-agent string."""
    return random.choice(_USER_AGENTS)


async def apply_stealth(page, user_agent: Optional[str] = None) -> None:
    """Apply fingerprint-smoothing patches to an existing Playwright page.

    Safe to call on every navigation. Each patch is idempotent.
    """
    ua = user_agent or random_user_agent()
    try:
        await page.set_extra_http_headers({"User-Agent": ua})
    except Exception:
        logger.debug("Could not set extra HTTP headers for stealth.")

    await page.evaluate(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
        """
    )

    await page.evaluate(
        """
        (plugins) => {
            Object.defineProperty(navigator, 'plugins', {
                get: () => plugins,
                configurable: true,
            });
        }
        """,
        _PLUGIN_DATA,
    )

    await page.evaluate(
        """
        (mimes) => {
            const handler = {
                get: () => mimes.map((m, i) => ({
                    type: m,
                    suffixes: 'pdf',
                    description: 'Portable Document Format',
                    enabledPlugin: {
                        name: 'PDF Viewer',
                        description: 'Portable Document Format',
                        length: 1,
                        isArray: () => false,
                    },
                })),
            };
            Object.defineProperty(navigator, 'mimeTypes', { get: () => handler.get(), configurable: true });
        }
        """,
        _MIME_TYPES,
    )

    await page.evaluate(
        """
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
            configurable: true,
        });
        """
    )

    await page.evaluate(
        """
        // Some detectors look for window.chrome being undefined on non-Chromium browsers.
        if (window.chrome && window.chrome.runtime) {
            try {
                delete window.chrome.runtime;
            } catch (e) { /* ignore */ }
        }
        """
    )

    try:
        await page.set_viewport_size({"width": 1366, "height": 768})
    except Exception:
        logger.debug("Could not set viewport size for stealth.")

    try:
        await page.evaluate(
            """
            const tz = 'America/New_York';
            Intl.DateTimeFormat().resolvedOptions().timeZone;
            """
        )
    except Exception:
        pass

    logger.debug("Stealth patches applied to page.")


async def init_context_stealth(context, user_agent: Optional[str] = None) -> None:
    """Apply stealth defaults to a whole browser context (new pages inherit them)."""
    ua = user_agent or random_user_agent()
    try:
        await context.set_extra_http_headers({"User-Agent": ua})
    except Exception:
        logger.debug("Could not set context-level extra HTTP headers.")
    try:
        await context.set_default_timeout(45000)
    except Exception:
        pass