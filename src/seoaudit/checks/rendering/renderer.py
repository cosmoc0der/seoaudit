from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

_VIEWPORTS = {True: {"width": 412, "height": 915}, False: {"width": 1366, "height": 900}}


class RenderUnavailable(RuntimeError):
    pass


@dataclass
class Rendered:
    url: str
    html: str


async def render(
    urls: list[str], user_agent: str, locale: str, mobile: bool, options: dict[str, Any]
) -> dict[str, Rendered | Exception]:
    """The DOM of each page after JavaScript ran, as headless Chromium sees it."""
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        from playwright.async_api import async_playwright
    except ImportError:
        raise RenderUnavailable('Playwright is not installed: pip install "seoaudit[browser]"') from None

    blocked = set(options["block"])

    async def route(route) -> None:
        # The DOM doesn't depend on pixels, and skipping them makes rendering several times faster.
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:
            raise RenderUnavailable(str(exc).strip().splitlines()[0]) from None
        try:
            context = await browser.new_context(
                user_agent=user_agent,
                # Without it Chromium takes the system locale, and sites that localize by it show another language.
                locale=locale,
                viewport=_VIEWPORTS[mobile],
                is_mobile=mobile,
                ignore_https_errors=True,
            )
            await context.route("**/*", route)
            semaphore = asyncio.Semaphore(options["concurrency"])

            async def one(url: str) -> Rendered | Exception:
                async with semaphore:
                    page = await context.new_page()
                    try:
                        await page.goto(url, wait_until="load", timeout=options["timeout"] * 1000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=options["settle_ms"])
                        except PlaywrightTimeout:
                            pass  # analytics and long polling keep the network busy forever
                        return Rendered(page.url, await page.content())
                    except Exception as exc:
                        return exc
                    finally:
                        await page.close()

            results = await asyncio.gather(*(one(u) for u in urls))
        finally:
            await browser.close()
    return dict(zip(urls, results))
