from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from seoaudit.core.context import AuditContext
from seoaudit.core.http import fetch
from seoaudit.core.page import Page

log = logging.getLogger(__name__)

PageHandler = Callable[[Page], Awaitable[None]]


class Crawler:
    def __init__(self, ctx: AuditContext, on_page: PageHandler):
        self.ctx = ctx
        self.cfg = ctx.config.crawl
        self.on_page = on_page
        self._queue: asyncio.Queue[tuple[str, int | None, str, str | None]] = asyncio.Queue()
        self._seen: set[str] = set()

    async def run(self) -> None:
        self._enqueue(self.ctx.site.base_url, 0, "start", None)
        await self._drain()
        if self.cfg.use_sitemap:
            for url in sorted(self.ctx.site.sitemap_urls):
                self._enqueue(url, None, "sitemap", None)
            await self._drain()

    async def _drain(self) -> None:
        if self._queue.empty():
            return
        workers = [asyncio.create_task(self._worker()) for _ in range(self.cfg.concurrency)]
        await self._queue.join()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def _enqueue(self, url: str, depth: int | None, source: str, referrer: str | None) -> None:
        if url in self._seen or len(self._seen) >= self.cfg.max_pages:
            return
        if not self.ctx.scope.is_crawlable(url):
            return
        if self.cfg.respect_robots and not self.ctx.site.can_fetch(url, self.cfg.robots_agent):
            self.ctx.blocked.add(url)
            return
        self._seen.add(url)
        self._queue.put_nowait((url, depth, source, referrer))

    async def _worker(self) -> None:
        while True:
            url, depth, source, referrer = await self._queue.get()
            try:
                await self._process(url, depth, source, referrer)
            except Exception as exc:
                log.exception("Failed to process %s", url)
                self.ctx.diagnostics.append(f"crawler: {url}: {exc}")
            finally:
                self._queue.task_done()
            if self.cfg.delay:
                await asyncio.sleep(self.cfg.delay)

    async def _process(self, url: str, depth: int | None, source: str, referrer: str | None) -> None:
        response = await fetch(self.ctx.client, url, max_bytes=int(self.cfg.max_body_mb * 1024 * 1024))
        pages: list[Page] = []

        if response.redirects:
            final = self.ctx.scope.normalize(response.final_url)
            if final and final not in self._seen and self.ctx.scope.is_crawlable(final):
                self._seen.add(final)
                pages.append(self._make_page(final, replace(response, url=final, redirects=[]), depth, source, referrer))
            pages.append(self._make_page(url, replace(response, body=b""), depth, source, referrer))
        else:
            pages.append(self._make_page(url, response, depth, source, referrer))

        for page in pages:
            self.ctx.pages[page.url] = page
            if page.ok:
                self._collect_links(page)
            await self.on_page(page)

    def _make_page(self, url, response, depth, source, referrer) -> Page:
        return Page(url=url, response=response, scope=self.ctx.scope, depth=depth, source=source, referrer=referrer)

    def _collect_links(self, page: Page) -> None:
        follow = self.cfg.follow_nofollow or "nofollow" not in page.robots
        next_depth = page.depth + 1 if page.depth is not None else None
        for link in page.links:
            if not link.internal:
                continue
            self.ctx.inlinks[link.url].append((page.url, link))
            if follow and (link.nofollow is False or self.cfg.follow_nofollow):
                if next_depth is None or next_depth <= self.cfg.max_depth:
                    self._enqueue(link.url, next_depth, "link", page.url)
