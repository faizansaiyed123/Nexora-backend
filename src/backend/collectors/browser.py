"""Generic Playwright renderer used only when HTTP HTML is not useful."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("nexora.browser")


async def render_page(url: str, timeout_seconds: int = 20) -> Optional[str]:
    """Return rendered HTML, or None when Playwright/browser is unavailable."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
                return await page.content()
            finally:
                await browser.close()
    except Exception as exc:
        logger.info("Browser rendering unavailable for %s: %s", url, exc)
        return None
