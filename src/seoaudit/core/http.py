from __future__ import annotations

import re
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import urljoin

import httpx

from seoaudit.core.config import CrawlConfig

_CHARSET_HEADER = re.compile(r"charset=[\"']?([\w-]+)", re.I)
_CHARSET_META = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)


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
        return self.error is not None or (self.status or 0) >= 400

    def text(self) -> str:
        return decode_body(self.body, self.headers.get("content-type", ""))


def create_client(
    cfg: CrawlConfig, verify: bool = True, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        verify=verify,
        transport=transport,
        timeout=httpx.Timeout(cfg.timeout),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=cfg.concurrency * 3, max_keepalive_connections=cfg.concurrency),
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
) -> FetchResult:
    result = FetchResult(url=url, final_url=url)
    started = perf_counter()
    current = url

    for _ in range(max_redirects + 1):
        hop_started = perf_counter()
        try:
            response = await _send(client, method, current)
        except Exception as exc:
            result.error = _describe(exc)
            return result

        try:
            if follow and response.is_redirect:
                result.redirects.append(Redirect(current, response.status_code))
                current = urljoin(current, response.headers["location"])
                continue

            result.final_url = current
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
        finally:
            await response.aclose()
            result.total_ms = (perf_counter() - started) * 1000
        return result

    result.error = f"Too many redirects (> {max_redirects})"
    return result


async def _send(client: httpx.AsyncClient, method: str, url: str, attempts: int = 2) -> httpx.Response:
    # h2 occasionally fails on reused connections closed by the server; one retry opens a fresh one
    for attempt in range(attempts):
        try:
            return await client.send(client.build_request(method, url), stream=True)
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
    match = _CHARSET_HEADER.search(content_type) or _CHARSET_META.search(body[:4096])
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
