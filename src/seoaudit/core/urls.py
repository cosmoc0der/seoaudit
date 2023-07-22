from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SKIP_SCHEMES = ("#", "javascript:", "mailto:", "tel:", "data:", "sms:", "skype:", "viber:", "tg:")
_PATH_SAFE = "/:@!$&'()*+,;=-._~"


def normalize_url(url: str, base: str | None = None, strip_params: tuple[str, ...] = ()) -> str | None:
    url = (url or "").strip()
    if not url or url.lower().startswith(_SKIP_SCHEMES):
        return None
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None

    host = parts.hostname.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = quote(_remove_dot_segments(unquote(parts.path)), safe=_PATH_SAFE) or "/"

    query = parts.query
    if query:
        pairs = [
            (k, v)
            for k, v in parse_qsl(query, keep_blank_values=True)
            if not any(fnmatch(k.lower(), p) for p in strip_params)
        ]
        query = urlencode(pairs, quote_via=quote)
    return urlunsplit((scheme, netloc, path, query, ""))


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
