from __future__ import annotations

from conftest import SITE
from seoaudit.core.http import FetchResult
from seoaudit.core.page import Page
from seoaudit.core.srcset import pick_candidate
from seoaudit.core.urls import UrlScope


def test_slot_width_from_sizes_picks_smallest_fitting_candidate():
    srcset = "full.png 1713w, s-300.png 300w, s-1024.png 1024w, s-768.png 768w, s-1536.png 1536w"
    assert pick_candidate(srcset, "(max-width: 767px) 100vw, (max-width: 991px) 95vw, 940px") == "s-1024.png"
    assert pick_candidate(srcset, "(max-width: 479px) 90vw, 25vw") == "s-768.png"
    assert pick_candidate(srcset, None) == "full.png"


def test_density_and_cdn_urls_with_commas():
    assert pick_candidate("a.jpg, a@2x.jpg 2x", None) == "a.jpg"
    assert pick_candidate("https://cdn.test/w_400,h_300/a.jpg 400w, https://cdn.test/w_2000,h_1500/a.jpg 2000w", "300px") == (
        "https://cdn.test/w_400,h_300/a.jpg"
    )


def test_picture_source_wins_over_img_src():
    html = (
        '<picture><source type="image/jxl" srcset="/a.jxl"><source type="image/avif" srcset="/a.avif">'
        '<img src="/a.png"></picture><img src="/b.png"><noscript><img src="/c.png"></noscript>'
    )
    page = Page(f"{SITE}/", FetchResult(url=f"{SITE}/", final_url=f"{SITE}/"), UrlScope("shop.test"), html=html)
    assert [i.delivered for i in page.images] == [f"{SITE}/a.avif", f"{SITE}/b.png"]
    assert page.images[0].variants == (f"{SITE}/a.png", f"{SITE}/a.jxl", f"{SITE}/a.avif")
