from __future__ import annotations

import httpx
import pytest

SITE = "http://shop.test"
DESCRIPTION = '<meta name="description" content="Same description on every page of the test shop website.">'


def html(title: str, body: str, head: str = DESCRIPTION) -> str:
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>{head}</head><body>{body}</body></html>"
    )


PAGES: dict[str, tuple[int, dict[str, str], bytes]] = {
    "/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private\n"),
    "/": (
        200,
        {"content-type": "text/html; charset=utf-8"},
        html(
            "Test shop home page with a reasonably long title",
            '<h1>Shop</h1><a href="/about">About us</a> <a href="/broken">Broken</a> '
            '<a href="/old">Old</a> <a href="/private/x">Private</a> <img src="/big.jpg">',
        ).encode(),
    ),
    "/about": (
        200,
        {"content-type": "text/html; charset=utf-8"},
        html("About the test shop and the people behind it", "<p>No heading here</p>").encode(),
    ),
    "/old": (301, {"location": "/about"}, b""),
    "/big.jpg": (200, {"content-type": "image/jpeg"}, b"\xff" * 300 * 1024),
}


def handler(request: httpx.Request) -> httpx.Response:
    status, headers, body = PAGES.get(request.url.path, (404, {"content-type": "text/html"}, b"<h1>404</h1>"))
    if request.url.host != "shop.test":
        return httpx.Response(404)
    return httpx.Response(status, headers=headers, content=b"" if request.method == "HEAD" else body)


@pytest.fixture
def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
