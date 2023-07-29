from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    AUDIT_STARTED = "audit.started"
    SITE_DISCOVERED = "site.discovered"
    PAGE_CRAWLED = "page.crawled"
    CRAWL_FINISHED = "crawl.finished"
    CHECK_STARTED = "check.started"
    CHECK_FINISHED = "check.finished"
    AUDIT_FINISHED = "audit.finished"


@dataclass(frozen=True)
class Event:
    type: EventType
    message: str = ""
    current: int | None = None
    total: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


EventHandler = Callable[[Event], None]
