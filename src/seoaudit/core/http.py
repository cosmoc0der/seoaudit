from __future__ import annotations

import asyncio
import codecs
import re
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import urljoin

import httpx

from seoaudit.core.config import CrawlConfig

_CHARSET_HEADER = re.compile(r"charset=[\"']?([\w-]+)", re.I)
_CHARSET_META = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)
_RETRY_DELAY = 2.0
# Browsers switch encoding on a late <meta charset> too, so look past the spec's 1024-byte prescan.
_META_SCAN_BYTES = 64 * 1024
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))


@dataclass
class Redirect:
    url: str
    status: int


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: list[str] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    body: bytes = b""
    size: int | None = None
    truncated: bool = False
    ttfb_ms: float | None = None
    total_ms: float | None = None
    http_version: str | None = None
    error: str | None = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def broken(self) -> bool:
        # A body that failed to download after a 2xx/3xx status is a transfer problem, not a dead link.
        return (self.status or 0) >= 400 or (self.status is None and self.error is not None)

    def text(self) -> str:
        return decode_body(self.body, self.headers.get("content-type", ""))


class _Http1Fallback(httpx.AsyncBaseTransport):
    """Some servers (GitBook) break every other h2 stream with GOAWAY under httpx; HTTP/1.1 to them just works.

    httpcore also overruns the server's stream limit under load ("Max outbound streams is 100") instead of
    queueing; that is our side's failure, never the resource's, so it gets the same fallback.
    """

    def __init__(self, verify: bool, limits: httpx.Limits):
        self._h2 = httpx.AsyncHTTPTransport(http2=True, verify=verify, limits=limits)
        self._h1 = httpx.AsyncHTTPTransport(verify=verify, limits=limits)
        self._h1_hosts: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in self._h1_hosts:
            try:
                return await self._h2.handle_async_request(request)
            except httpx.ProtocolError:
                self._h1_hosts.add(request.url.host)
                raise
        return await self._h1.handle_async_request(request)

    async def aclose(self) -> None:
        await self._h2.aclose()
        await self._h1.aclose()


def create_client(
    cfg: CrawlConfig, verify: bool = True, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=cfg.concurrency * 3, max_keepalive_connections=cfg.concurrency)
    return httpx.AsyncClient(
        verify=verify,
        transport=transport or _Http1Fallback(verify, limits),
        timeout=httpx.Timeout(cfg.timeout),
        follow_redirects=False,
        headers={
            "User-Agent": cfg.effective_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    read_body: bool = True,
    keep_body: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    max_redirects: int = 10,
    follow: bool = True,
    attempts: int = 2,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    # Slow sites time out under crawl load and answer fine moments later.
    for attempt in range(1, attempts + 1):
        result, timed_out = await _fetch_once(
            client, url, method, read_body, keep_body, max_bytes, max_redirects, follow, headers
        )
        if not timed_out or attempt == attempts:
            return result
        await asyncio.sleep(_RETRY_DELAY * attempt)
    raise AssertionError("unreachable")


async def _fetch_once(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    read_body: bool,
    keep_body: bool,
    max_bytes: int,
    max_redirects: int,
    follow: bool,
    headers: dict[str, str] | None,
) -> tuple[FetchResult, bool]:
    result = FetchResult(url=url, final_url=url)
    started = perf_counter()
    current = url

    for _ in range(max_redirects + 1):
        hop_started = perf_counter()
        result.final_url = current
        try:
            response = await _send(client, method, current, headers)
        except Exception as exc:
            result.error = _describe(exc)
            return result, isinstance(exc, httpx.TimeoutException)

        try:
            # httpx calls any 3xx a redirect; without Location the browser just shows that response.
            if follow and response.is_redirect and "location" in response.headers:
                result.redirects.append(Redirect(current, response.status_code))
                current = urljoin(current, response.headers["location"])
                continue

            result.status = response.status_code
            result.http_version = response.http_version
            result.ttfb_ms = (perf_counter() - hop_started) * 1000
            result.set_cookies = response.headers.get_list("set-cookie")
            result.headers = {k.lower(): v for k, v in response.headers.items() if k.lower() != "set-cookie"}

            if read_body:
                await _read_body(response, result, keep_body, max_bytes)
            elif length := response.headers.get("content-length"):
                result.size = int(length) if length.isdigit() else None
        except Exception as exc:
            result.error = _describe(exc)
            return result, isinstance(exc, httpx.TimeoutException)
        finally:
            await response.aclose()
            result.total_ms = (perf_counter() - started) * 1000
        return result, False

    result.final_url = current
    result.error = f"Too many redirects (> {max_redirects})"
    return result, False


async def _send(
    client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str] | None, attempts: int = 2
) -> httpx.Response:
    # After an h2 protocol failure the transport switches the host to HTTP/1.1, so the retry goes there.
    for attempt in range(attempts):
        try:
            return await client.send(client.build_request(method, url, headers=headers), stream=True)
        except (httpx.TimeoutException, httpx.UnsupportedProtocol, httpx.InvalidURL, ValueError):
            raise
        except Exception:
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


async def _read_body(response: httpx.Response, result: FetchResult, keep: bool, max_bytes: int) -> None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if keep:
            chunks.append(chunk)
        if size > max_bytes:
            result.truncated = True
            break
    result.size = size
    result.body = b"".join(chunks)


def decode_body(body: bytes, content_type: str) -> str:
    # A BOM beats both the header and <meta>, as in browsers; robots.txt parsers choke on a leftover one.
    for bom, encoding in _BOMS:
        if body.startswith(bom):
            return body[len(bom) :].decode(encoding, errors="replace")
    match = _CHARSET_HEADER.search(content_type) or _CHARSET_META.search(body[:_META_SCAN_BYTES])
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", "ignore")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _describe(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
