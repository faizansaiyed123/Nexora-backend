"""Generic Playwright renderer used when HTTP HTML is not useful.

Applies stealth fingerprint-smoothing patches and optionally tunnels traffic
through a configured residential proxy. Falls back to None when Playwright is
unavailable so callers can degrade gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.core.config import get_settings
from backend.collectors.stealth import apply_stealth, init_context_stealth, random_user_agent
from backend.services.url_security import SecurityValidationError, UrlSecurityService
from backend.services.browser_proxy import browser_proxy

logger = logging.getLogger("nexora.browser")
_settings = get_settings()


async def _validate_url_async(url: str) -> str:
    """Run the blocking SSRF validation in a thread so the event loop is not stalled."""
    return await asyncio.to_thread(UrlSecurityService.validate_url, url)


async def render_page(url: str, timeout_seconds: int = 20) -> Optional[str]:
        """Return rendered HTML, or None when Playwright/browser is unavailable."""
        try:
            from playwright.async_api import async_playwright

            safe_url = await _validate_url_async(url)
            ua = random_user_agent()
            proxy_url = _settings.residential_proxy_url or None
            async with browser_proxy(residential_proxy_url=proxy_url) as proxy:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        headless=True,
                        proxy={"server": proxy.url} if proxy else None,
                    )
                    try:
                        context = await browser.new_context(user_agent=ua)
                        await init_context_stealth(context, user_agent=ua)
                        page = await context.new_page()
                        await apply_stealth(page, user_agent=ua)

                        async def guard_route(route) -> None:
                            try:
                                await _validate_url_async(route.request.url)
                            except SecurityValidationError:
                                await route.abort()
                                return
                            await route.continue_()

                        await page.route("**/*", guard_route)
                        await page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=timeout_seconds * 1000)
                        except Exception:
                            # networkidle can hang on long-polling pages; domcontentloaded is enough.
                            pass
                        return await page.content()
                    finally:
                        try:
                            await asyncio.wait_for(browser.close(), timeout=5.0)
                        except Exception:
                            pass
        except Exception as exc:
            logger.info("Browser rendering unavailable for %s: %s", url, exc)
            return None