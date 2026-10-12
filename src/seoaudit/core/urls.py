from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from functools import lru_cache
from urllib.parse import quote, unquote, unquote_plus, urljoin, urlsplit, urlunsplit

import idna

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SKIP_SCHEMES = ("#", "javascript:", "mailto:", "tel:", "data:", "sms:", "skype:", "viber:", "tg:")
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"
_QUERY_SAFE = _PATH_SAFE + "?"
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_LONE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# Cloudflare serves its own endpoints here (email obfuscation, challenges); the site owner doesn't control them.
_SERVICE_PATH = "/cdn-cgi/"


def normalize_url(url: str, base: str | None = None, strip_params: tuple[str, ...] = ()) -> str | None:
    url = (url or "").strip()
    if not url or url.lower().startswith(_SKIP_SCHEMES):
        return None
    return _normalize_absolute(urljoin(base, url) if base else url, strip_params)


@lru_cache(maxsize=100_000)
def _normalize_absolute(url: str, strip_params: tuple[str, ...]) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None

    host = _encode_host(parts.hostname.lower().rstrip("."))
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    # Escapes are kept as is: decoding would mangle non-UTF-8 bytes (legacy cp1251 URLs) and %2F.
    path = quote(_remove_dot_segments(_normalize_escapes(parts.path)), safe=_PATH_SAFE) or "/"

    query = parts.query
    if query and strip_params:
        query = "&".join(p for p in query.split("&") if not _is_stripped(p, strip_params))
    query = quote(_normalize_escapes(query), safe=_QUERY_SAFE)
    return urlunsplit((scheme, netloc, path, query, ""))


def _encode_host(host: str) -> str:
    if host.isascii():
        return host
    # Browsers use UTS #46 (IDNA 2008): the stdlib codec is IDNA 2003 and maps "faß.de" to "fass.de".
    try:
        return idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        pass
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _normalize_escapes(text: str) -> str:
    """RFC 3986 6.2.2: uppercase hex, decode unreserved characters, escape stray '%'."""
    if "%" not in text:
        return text

    def fix(match: re.Match) -> str:
        char = chr(int(match[1], 16))
        return char if char in _UNRESERVED else f"%{match[1].upper()}"

    return _ESCAPE.sub(fix, _LONE_PERCENT.sub("%25", text))


def _is_stripped(pair: str, patterns: tuple[str, ...]) -> bool:
    key = unquote_plus(pair.split("=", 1)[0]).lower()
    return any(fnmatch(key, p) for p in patterns)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4: urljoin resolves dots only for relative refs, absolute links keep them."""
    if "/." not in path:
        return path
    output: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(output) > 1:
                output.pop()
        elif segment != ".":
            output.append(segment)
    if path.endswith(("/.", "/..")):
        output.append("")
    return "/".join(output) or "/"


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_service_url(url: str) -> bool:
    return urlsplit(url).path.startswith(_SERVICE_PATH)


def pretty_url(url: str) -> str:
    """Human-readable form: decoded percent-escapes and IDN (punycode) hosts."""
    if "xn--" in url:
        try:
            parts = urlsplit(url)
            if parts.hostname:
                netloc = _decode_idna(parts.hostname) + (f":{parts.port}" if parts.port else "")
                url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            else:
                url = _decode_idna(url)
        except (UnicodeError, ValueError):
            pass
    return unquote(url)


def _decode_idna(host: str) -> str:
    return host.encode("ascii").decode("idna")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class UrlScope:
    """Decides which URLs belong to the audited site."""

    root_host: str
    include_subdomains: bool = False
    strip_params: tuple[str, ...] = ()
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._root = strip_www(self.root_host)
        self._include = [re.compile(p) for p in self.include]
        self._exclude = [re.compile(p) for p in self.exclude]

    def normalize(self, url: str, base: str | None = None) -> str | None:
        return normalize_url(url, base, self.strip_params)

    def is_internal(self, url: str) -> bool:
        host = strip_www(host_of(url))
        return host == self._root or (self.include_subdomains and host.endswith("." + self._root))

    def is_crawlable(self, url: str) -> bool:
        if not self.is_internal(url):
            return False
        if self._include and not any(p.search(url) for p in self._include):
            return False
        return not any(p.search(url) for p in self._exclude)
