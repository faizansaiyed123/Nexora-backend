"""Generic Playwright renderer used only when HTTP HTML is not useful."""

from __future__ import annotations

import logging
from typing import Optional

from backend.services.url_security import SecurityValidationError, UrlSecurityService
from backend.services.browser_proxy import browser_proxy

logger = logging.getLogger("nexora.browser")


async def render_page(url: str, timeout_seconds: int = 20) -> Optional[str]:
    """Return rendered HTML, or None when Playwright/browser is unavailable."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            safe_url = UrlSecurityService.validate_url(url)
            async with browser_proxy() as proxy:
                browser = await playwright.chromium.launch(
                    headless=True,
                    proxy={"server": proxy.url},
                )
                try:
                    page = await browser.new_page()

                async def guard_route(route) -> None:
                    try:
                        UrlSecurityService.validate_url(route.request.url)
                    except SecurityValidationError:
                        await route.abort()
                        return
                    await route.continue_()

                await page.route("**/*", guard_route)
                await page.goto(safe_url, wait_until="networkidle", timeout=timeout_seconds * 1000)
                    return await page.content()
                finally:
                    await browser.close()
    except Exception as exc:
        logger.info("Browser rendering unavailable for %s: %s", url, exc)
        return None
