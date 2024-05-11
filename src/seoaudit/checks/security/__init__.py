from __future__ import annotations

import asyncio
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from seoaudit.checks._utils import report_group
from seoaudit.core.check import Check
from seoaudit.core.http import fetch
from seoaudit.core.page import Page

_VERSION_IN_HEADER = re.compile(r"\d+\.\d+")
_DISCLOSURE_HEADERS = ("x-powered-by", "x-aspnet-version", "x-aspnetmvc-version")
_HEADER_RULES = {
    "hsts_missing": "strict-transport-security",
    "csp_missing": "content-security-policy",
    "content_type_options_missing": "x-content-type-options",
    "referrer_policy_missing": "referrer-policy",
    "permissions_policy_missing": "permissions-policy",
}


class SecurityCheck(Check):
    id = "security"

    async def setup(self) -> None:
        self._cookies: dict[str, tuple[str, list[str]]] = {}

    async def check_page(self, page: Page) -> None:
        if page.url.startswith("https://"):
            resources = [*(i.src for i in page.images), *(s.src for s in page.scripts), *page.stylesheets]
            resources += [page.absolute(n.attributes.get("src")) for n in page.tree.css("iframe[src], video[src], audio[src], source[src]")]
            report_group(self, "mixed_content", page.url, [u for u in resources if u and u.startswith("http://")])
        report_group(
            self,
            "target_blank_noopener",
            page.url,
            [l.url for l in page.links if l.target == "_blank" and not l.internal and not {"noopener", "noreferrer"} & l.rel],
        )
        for raw in page.response.set_cookies:
            name, issues = _cookie_issues(raw, page.url.startswith("https://"))
            if issues and name not in self._cookies:
                self._cookies[name] = (page.url, issues)

    async def check_site(self) -> None:
        site = self.ctx.site
        is_https = site.base_url.startswith("https://")
        if not is_https:
            self.fail("https_missing", site.base_url)
            for rule in ("http_redirect", "ssl_invalid", "ssl_expiring", "hsts_missing"):
                self.not_applicable(rule, "not_https")
        else:
            await asyncio.gather(self._http_redirect(), self._certificate())

        self._headers(is_https)
        for name, (url, issues) in self._cookies.items():
            self.fail("cookies_insecure", url, cookie=name, issues=issues)
        await self._sensitive_files()

    async def _http_redirect(self) -> None:
        host = urlsplit(self.ctx.site.base_url).netloc
        result = await fetch(self.ctx.client, f"http://{host}/", follow=False, read_body=False)
        if result.error or result.status is None:
            self.not_applicable("http_redirect", "port_80_closed")
            return
        location = result.headers.get("location", "")
        if result.status in (301, 308) and location.startswith("https://"):
            return
        key = "temporary" if result.status in (302, 303, 307) and location.startswith("https://") else "none"
        self.fail("http_redirect", f"http://{host}/", key=key, status=result.status)

    async def _certificate(self) -> None:
        site = self.ctx.site
        parts = urlsplit(site.base_url)
        if site.tls_error:
            self.fail("ssl_invalid", site.base_url, error=site.tls_error)
            self.not_applicable("ssl_expiring")
            return
        try:
            expires_at = await asyncio.to_thread(_cert_expiry, parts.hostname, parts.port or 443, self.ctx.config.crawl.timeout)
        except ssl.SSLCertVerificationError as exc:
            self.fail("ssl_invalid", site.base_url, error=exc.verify_message or str(exc))
            return
        except (OSError, ssl.SSLError) as exc:
            self.error("ssl_expiring", f"TLS handshake failed: {exc}")
            return
        days = int((expires_at - time.time()) // 86400)
        if days < self.params("ssl_expiring")["min_days"]:
            self.fail("ssl_expiring", site.base_url, days=days)

    def _headers(self, is_https: bool) -> None:
        home = self.ctx.site.home
        headers = home.headers
        url = self.ctx.site.base_url
        for rule, header in _HEADER_RULES.items():
            if rule == "hsts_missing" and not is_https:
                continue
            if not headers.get(header):
                self.fail(rule, url)
        csp = headers.get("content-security-policy", "").lower()
        if not headers.get("x-frame-options") and "frame-ancestors" not in csp:
            self.fail("clickjacking_protection_missing", url)

        exposed = [f"{h}: {headers[h]}" for h in _DISCLOSURE_HEADERS if headers.get(h)]
        server = headers.get("server", "")
        if _VERSION_IN_HEADER.search(server):
            exposed.append(f"server: {server}")
        if exposed:
            self.fail("server_disclosure", url, headers=exposed)

    async def _sensitive_files(self) -> None:
        paths: dict[str, str] = self.params("sensitive_files")["paths"]
        origin = self.ctx.site.origin

        async def probe(path: str, pattern: str) -> None:
            result = await fetch(self.ctx.client, origin + path, follow=False, max_bytes=64 * 1024)
            if result.status == 200 and "text/html" not in result.content_type:
                if re.search(pattern.encode(), result.body, re.MULTILINE):
                    self.fail("sensitive_files", origin + path, path=path)

        await asyncio.gather(*(probe(path, pattern) for path, pattern in paths.items()))


def _cookie_issues(raw: str, is_https: bool) -> tuple[str, list[str]]:
    name = raw.split("=", 1)[0].strip()
    attrs = {part.strip().split("=", 1)[0].lower() for part in raw.split(";")[1:]}
    issues = []
    if is_https and "secure" not in attrs:
        issues.append("Secure")
    if "httponly" not in attrs:
        issues.append("HttpOnly")
    if "samesite" not in attrs:
        issues.append("SameSite")
    return name, issues


def _cert_expiry(host: str, port: int, timeout: float) -> float:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            return ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
