from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from seoaudit.core.context import AuditContext
from seoaudit.core.http import fetch
from seoaudit.core.page import Page
from seoaudit.core.urls import origin_of

log = logging.getLogger(__name__)

PageHandler = Callable[[Page], Awaitable[None]]


class Crawler:
    def __init__(self, ctx: AuditContext, on_page: PageHandler):
        self.ctx = ctx
        self.cfg = ctx.config.crawl
        self.on_page = on_page
        self._queue: asyncio.Queue[tuple[str, int | None, str, str | None]] = asyncio.Queue()
        self._seen: set[str] = set()
        self._next_level: dict[str, str] = {}

    async def run(self) -> None:
        # With robots.txt answering 5xx everything is disallowed; the start page still gives the audit a subject.
        self._enqueue(self.ctx.site.base_url, 0, "start", None, check_robots=not self.ctx.site.robots_unavailable)
        depth = 0
        while not self._queue.empty():
            await self._drain()
            # One level at a time: with concurrent workers a page could otherwise be reached by a longer path first.
            depth += 1
            pending, self._next_level = self._next_level, {}
            if depth <= self.cfg.max_depth:
                await self._load_robots(list(pending))
                for url, referrer in pending.items():
                    self._enqueue(url, depth, "link", referrer)

        if self.cfg.use_sitemap:
            urls = sorted(self.ctx.site.sitemap_urls)
            await self._load_robots(urls)
            for url in urls:
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

    async def _load_robots(self, urls: list[str]) -> None:
        if not self.cfg.respect_robots:
            return
        by_origin = {origin_of(u): u for u in urls if self.ctx.scope.is_crawlable(u)}
        await asyncio.gather(*(self.ctx.site.load_robots(self.ctx.client, u) for u in by_origin.values()))

    def _admit(self, url: str, check_robots: bool = True) -> bool:
        """Marks the URL as taken if it may be crawled; robots.txt of its origin must be loaded already."""
        if url in self._seen or len(self._seen) >= self.cfg.max_pages:
            return False
        if not self.ctx.scope.is_crawlable(url):
            return False
        if check_robots and self.cfg.respect_robots and not self.ctx.site.can_fetch(url, self.cfg.robots_agent):
            self.ctx.blocked.add(url)
            return False
        self._seen.add(url)
        return True

    def _enqueue(
        self, url: str, depth: int | None, source: str, referrer: str | None, check_robots: bool = True
    ) -> None:
        if self._admit(url, check_robots):
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
            if final and response.error is None:
                await self._load_robots([final])
                if self._admit(final):
                    target = replace(response, url=final, redirects=[])
                    pages.append(self._make_page(final, target, depth, source, referrer))
            pages.append(self._make_page(url, replace(response, body=b""), depth, source, referrer))
        else:
            pages.append(self._make_page(url, response, depth, source, referrer))

        for page in pages:
            self.ctx.pages[page.url] = page
            if page.ok:
                await self._collect_links(page)
            await self.on_page(page)

    def _make_page(self, url, response, depth, source, referrer) -> Page:
        return Page(
            url=url,
            response=response,
            scope=self.ctx.scope,
            depth=depth,
            source=source,
            referrer=referrer,
            robots_agent=self.cfg.robots_agent,
        )

    async def _collect_links(self, page: Page) -> None:
        follow = self.cfg.follow_nofollow or "nofollow" not in page.robots
        targets = []
        for link in page.links:
            if not link.internal:
                continue
            # A page linking to itself doesn't make it reachable (orphan detection relies on this).
            if link.url != page.url:
                self.ctx.inlinks[link.url].append((page.url, link))
            if follow and (link.nofollow is False or self.cfg.follow_nofollow):
                targets.append(link.url)

        if page.depth is not None:
            for url in targets:
                if url not in self._seen:
                    self._next_level.setdefault(url, page.url)
            return
        await self._load_robots(targets)
        for url in targets:
            self._enqueue(url, None, "link", page.url)
