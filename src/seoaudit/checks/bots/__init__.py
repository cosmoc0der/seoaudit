from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from seoaudit.checks._utils import changed_signals, truncate
from seoaudit.core.check import Check
from seoaudit.core.http import FetchResult, fetch
from seoaudit.core.page import Page
from seoaudit.core.urls import pretty_url

_ACCESS_RULES = ("search_bot_denied", "ai_search_blocked", "ai_training_blocked")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")
_PAYWALL = re.compile(r'"isAccessibleForFree"\s*:\s*"?false', re.I)


class BotsCheck(Check):
    """How search and AI crawlers see the site: robots.txt groups, server answers to their User-Agent, cloaking."""

    id = "bots"

    async def check_site(self) -> None:
        self._semaphore = asyncio.Semaphore(self.ctx.config.crawl.concurrency)
        await asyncio.gather(self._access(), self._cloaking(), self._llms_txt())

    async def _get(self, url: str, agent: str | None, read_body: bool = True) -> FetchResult:
        async with self._semaphore:
            return await fetch(self.ctx.client, url, read_body=read_body, headers={"User-Agent": self._user_agent(agent)})

    def _user_agent(self, agent: str | None) -> str:
        if agent is None:
            return self.options["browser_agent"]
        return self.options["user_agents"].get(agent) or self.options["user_agent_template"].format(agent=agent)

    async def _access(self) -> None:
        site = self.ctx.site
        home, robots_url = site.base_url, f"{site.origin}/robots.txt"
        groups = {rule: self.params(rule)["agents"] for rule in _ACCESS_RULES}
        fetched = list(dict.fromkeys(a for agents in groups.values() for a in agents if a not in self.options["robots_only"]))
        baseline, *answers = await asyncio.gather(
            self._get(home, None, read_body=False), *(self._get(home, a, read_body=False) for a in fetched)
        )
        denied = {}
        if refused := _denial(baseline):
            self.ctx.diagnostics.append(f"bots: {home} refuses a browser too ({refused}), bot answers not compared")
        else:
            denied = {agent: reason for agent, answer in zip(fetched, answers) if (reason := _denial(answer))}

        for rule, agents in groups.items():
            for agent in agents:
                if agent in denied:
                    # A refused search bot is search_bot_denied; repeating it under another rule doubles the penalty.
                    if rule == "search_bot_denied" or agent not in groups["search_bot_denied"]:
                        self.fail(rule, home, key="server", agent=agent, status=denied[agent])
                # Googlebot and Yandex in robots.txt are indexability.robots_blocks_all.
                elif rule != "search_bot_denied" and site.robots_parser and not site.robots_parser.can_fetch(home, agent):
                    self.fail(rule, robots_url, key="robots", agent=agent)

    async def _cloaking(self) -> None:
        params = self.params("cloaking")
        agent = params["agent"]

        async def compare(url: str) -> None:
            browser, bot = await asyncio.gather(self._page(url, None), self._page(url, agent))
            # A refusal is search_bot_denied; here only a different page counts.
            if _denial(bot.response):
                return
            found = _differences(browser, bot, params)
            if found:
                # Rotating blocks make any two loads differ; only what a second browser load confirms is kept.
                again = {key for key, _ in _differences(await self._page(url, None), bot, params)}
                found = [(key, data) for key, data in found if key in again]
            for key, data in found:
                self.fail("cloaking", url, key=key, agent=agent, **data)

        await asyncio.gather(*(compare(u) for u in self.ctx.key_pages(self.options["sample_pages"])))

    async def _page(self, url: str, agent: str | None) -> Page:
        response = await self._get(url, agent)
        return Page(url=url, response=response, scope=self.ctx.scope, robots_agent=self.ctx.config.crawl.robots_agent)

    async def _llms_txt(self) -> None:
        url = f"{self.ctx.site.origin}/llms.txt"
        response = await fetch(self.ctx.client, url, max_bytes=1024 * 1024)
        text = response.text() if response.ok else ""
        # Catch-all routes answer any path with an HTML page.
        if not response.ok or "html" in response.content_type or text.lstrip().startswith("<"):
            self.fail("llms_txt_missing", url)
            return
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first.startswith("# "):
            self.fail("llms_txt_invalid", url, key="no_title", value=truncate(first, 60) or "—")
        links = list(dict.fromkeys(u for raw in _MARKDOWN_LINK.findall(text) if (u := self.ctx.scope.normalize(raw, url))))
        if not links:
            self.fail("llms_txt_invalid", url, key="no_links")
            return
        internal = [u for u in links if self.ctx.scope.is_internal(u)][: self.params("llms_txt_invalid")["max_probes"]]
        results = await self.ctx.prober.probe_many(internal)
        if broken := [u for u, result in results.items() if result.broken]:
            self.fail("llms_txt_invalid", url, key="broken", count=len(broken), examples=[pretty_url(u) for u in broken[:5]])


def _denial(result: FetchResult) -> str | None:
    if result.error:
        return result.error
    if challenge := result.headers.get("cf-mitigated"):
        return f"{result.status} (cf-mitigated: {challenge})"
    return str(result.status) if (result.status or 0) >= 400 else None


def _answer(page: Page) -> str:
    response = page.response
    return f"{response.status} → {pretty_url(response.final_url)}" if response.redirects else str(response.status)


def _differences(browser: Page, bot: Page, params: dict) -> list[tuple[str, dict]]:
    if _answer(browser) != _answer(bot):
        return [("status", {"browser": _answer(browser), "bot": _answer(bot)})]
    # A JS app served prerendered to bots (dynamic rendering) is allowed as long as the content matches.
    if not (browser.ok and bot.ok) or browser.word_count < params["min_words"]:
        return []
    found = [(key, {"browser": before, "bot": after}) for key, before, after in changed_signals(browser, bot)]
    # Google lets paywalled sites serve the full text to Googlebot when the markup declares the paywall.
    if any(_PAYWALL.search(block) for block in bot.jsonld_blocks):
        return found
    fewer, more = sorted((browser.word_count, bot.word_count))
    if more > fewer * params["max_text_ratio"]:
        found.append(("text", {"browser": browser.word_count, "bot": bot.word_count}))
    extra = _external_hosts(bot) - _external_hosts(browser)
    if len(extra) >= params["min_new_hosts"]:
        found.append(("links", {"count": len(extra), "hosts": sorted(extra)[:5]}))
    return found


def _external_hosts(page: Page) -> set[str]:
    return {host for link in page.links if not link.internal and (host := urlsplit(link.url).hostname)}
