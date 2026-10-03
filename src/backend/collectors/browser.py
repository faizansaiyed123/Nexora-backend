"""Generic Playwright renderer used only when HTTP HTML is not useful."""

from __future__ import annotations

import logging
from typing import Optional

from backend.services.url_security import SecurityValidationError, UrlSecurityService

logger = logging.getLogger("nexora.browser")


async def render_page(url: str, timeout_seconds: int = 20) -> Optional[str]:
    """Return rendered HTML, or None when Playwright/browser is unavailable."""
    try:
        from playwright.async_api import async_playwright

        validated = UrlSecurityService.resolve_and_validate_url(url, allow_empty=False)
        if validated is None:
            return None

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[f"--host-resolver-rules=MAP {validated.hostname} {validated.ip_address}"],
            )
            try:
                safe_url = validated.url
                page = await browser.new_page()

                async def guard_route(route) -> None:
                    try:
                        request_target = UrlSecurityService.resolve_and_validate_url(
                            route.request.url,
                            allow_empty=False,
                        )
                        if request_target is None or request_target.hostname != validated.hostname:
                            raise SecurityValidationError("Cross-host browser requests are not permitted.")
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
