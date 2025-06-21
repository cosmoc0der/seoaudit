from seoaudit.core.check import Check
from seoaudit.core.page import Page


class AnalyticsCheck(Check):
    id = "analytics"

    async def check_page(self, page: Page) -> None:
        html = (page.html or "").lower()
        counters = self.params("counter_missing")["counters"]
        if not any(marker.lower() in html for marker in counters.values()):
            self.fail("counter_missing", page.url, expected=list(counters))
