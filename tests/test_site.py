from __future__ import annotations

import httpx

from conftest import SITE
from seoaudit.core.site import _load_sitemaps
from seoaudit.core.urls import UrlScope

SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url><loc>http://shop.test/story/</loc><image:image><image:loc>http://shop.test/card.webp</image:loc></image:image></url>
</urlset>"""


async def test_sitemap_skips_image_locs():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "application/xml"}, content=SITEMAP))
    async with httpx.AsyncClient(transport=transport) as client:
        sitemaps, _ = await _load_sitemaps(client, [f"{SITE}/sitemap.xml"], set(), 100, UrlScope("shop.test"))
    assert sitemaps[0].urls == [f"{SITE}/story/"]
