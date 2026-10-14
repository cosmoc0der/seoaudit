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
# HttpOnly protects what a stolen session is made of; language or consent cookies are often read by scripts on purpose.
_SESSION_COOKIE = re.compile(r"sess|auth|token|login|jwt|(^|_)sid$", re.I)
# Double-submit CSRF tokens (XSRF-TOKEN, csrftoken) must stay readable by the page's own scripts.
_CSRF_COOKIE = re.compile(r"csrf|xsrf", re.I)
# These leak as ordinary HTML pages; for the rest an HTML answer is a catch-all page, not the file.
_HTML_LEAKS = {"/phpinfo.php", "/info.php", "/server-status"}
_TLS_ERRORS = (
    ("expired", "expired"),
    ("not yet valid", "not_yet_valid"),
    ("hostname mismatch", "wrong_host"),
    ("not valid for", "wrong_host"),
    ("self-signed", "self_signed"),
    ("self signed", "self_signed"),
    ("issuer", "missing_chain"),
)
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
            resources = [*(u for i in page.images for u in i.variants), *(s.src for s in page.scripts), *page.stylesheets]
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
        url = f"http://{urlsplit(self.ctx.site.base_url).netloc}/"
        result = await fetch(self.ctx.client, url, read_body=False)
        hops = result.redirects
        if not hops:
            if result.error or result.status is None:
                self.not_applicable("http_redirect", "port_80_closed")
            else:
                self.fail("http_redirect", url, key="none", status=result.status)
            return
        status = hops[0].status
        if not result.final_url.startswith("https://"):
            self.fail("http_redirect", url, key="none", status=status)
        elif status not in (301, 308):
            self.fail("http_redirect", url, key="temporary", status=status)
        elif any(hop.url.startswith("http://") for hop in hops[1:]):
            self.fail("http_redirect", url, key="chain", status=status)

    async def _certificate(self) -> None:
        site = self.ctx.site
        parts = urlsplit(site.base_url)
        if site.tls_error:
            self.fail("ssl_invalid", site.base_url, error=_tls_error(site.tls_error))
            self.not_applicable("ssl_expiring")
            return
        try:
            expires_at = await asyncio.to_thread(_cert_expiry, parts.hostname, parts.port or 443, self.ctx.config.crawl.timeout)
        except ssl.SSLCertVerificationError as exc:
            self.fail("ssl_invalid", site.base_url, error=_tls_error(exc.verify_message or str(exc)))
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
            if result.status == 200 and (path in _HTML_LEAKS or "text/html" not in result.content_type):
                if re.search(pattern.encode(), result.body, re.MULTILINE):
                    self.fail("sensitive_files", origin + path, path=path)

        await asyncio.gather(*(probe(path, pattern) for path, pattern in paths.items()))


def _cookie_issues(raw: str, is_https: bool) -> tuple[str, list[str]]:
    name = raw.split("=", 1)[0].strip()
    attrs = {part.strip().split("=", 1)[0].lower() for part in raw.split(";")[1:]}
    issues = []
    if is_https and "secure" not in attrs:
        issues.append("Secure")
    if "httponly" not in attrs and _SESSION_COOKIE.search(name) and not _CSRF_COOKIE.search(name):
        issues.append("HttpOnly")
    if "samesite" not in attrs:
        issues.append("SameSite")
    return name, issues


def _tls_error(raw: str) -> str:
    """OpenSSL wording means nothing to the report's reader; the cause does."""
    lowered = raw.lower()
    return next((code for marker, code in _TLS_ERRORS if marker in lowered), "untrusted")


def _cert_expiry(host: str, port: int, timeout: float) -> float:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            return ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
