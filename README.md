# seoaudit

Модульный SEO-аудитор сайтов: краулер + плагины проверок + PDF-отчёт с рекомендациями
и инструкциями для разработчиков.

- **106 правил в 12 модулях**: индексация, мета-теги, контент, ссылки, изображения, скорость,
  PageSpeed/Core Web Vitals, мобильная версия, безопасность, валидность HTML, микроразметка, URL, hreflang.
- **Правила — это данные.** Пороги, важность и тексты лежат в YAML рядом с модулем и
  переопределяются в конфиге без правки кода.
- **Отчёты:** PDF, HTML, JSON (полная модель для API/SaaS), CSV (для разработчиков и Excel).

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[browser,dev]"
playwright install --only-shell chromium   # Chromium без окна: PDF и локальный Lighthouse
```

Extra `browser` (Playwright) нужен только для PDF. Без него аудит работает и выдаёт HTML, JSON и CSV.
Если PDF стоит в форматах по умолчанию, вместо него будет записан HTML. Если PDF запрошен явно
(`-f pdf`), CLI сразу, ещё до сканирования, подскажет, что установить.

## Использование

```bash
seoaudit audit https://example.com                         # PDF + JSON в ./reports
seoaudit audit example.com -c config.yaml -f pdf,html,csv --max-pages 500
seoaudit audit example.com -d pagespeed,html               # отключить модули
seoaudit render reports/example.com-20250905-1200.json -f pdf   # перегенерировать отчёт без пересканирования
seoaudit rules                                              # все правила с текущими порогами
seoaudit checks                                             # подключённые модули
```

### Скорость: PageSpeed API или локальный Lighthouse

Модуль `pagespeed` берёт данные Lighthouse из одного из двух источников (`checks.pagespeed.options.provider`):

| | `api` — PageSpeed Insights | `local` — Lighthouse на этой машине |
|---|---|---|
| Что нужно | бесплатный ключ `PAGESPEED_API_KEY` | Node.js 22+ |
| Данные реальных пользователей (CrUX) | есть | нет, правило `field_cwv` помечается «неприменимо» |
| Закрытые сайты (staging, localhost, VPN) | нет | да |
| Скорость | несколько страниц параллельно | ~25 с на страницу, строго по очереди |
| Стабильность оценок | одинаковые серверы Google | зависит от загрузки машины |

По умолчанию `provider: auto`: при наличии ключа используется API, иначе локальный Lighthouse.
Lighthouse ищется в глобальной установке (`npm i -g lighthouse`). Если его там нет, при первом
запуске он ставится в `~/.cache/seoaudit/lighthouse`; другую папку задаёт переменная `SEOAUDIT_CACHE`.
Chromium берётся из Playwright, `CHROME_PATH` или системного Chrome. При запуске в Docker от root
добавьте `chrome_flags: "--no-sandbox"`.

Из Python:

```python
from pathlib import Path

from seoaudit import Auditor, load_config
from seoaudit.reporting import render_report

config = load_config("config.yaml")
report = Auditor(config, on_event=print).run_sync("https://example.com")
render_report(report, Path("reports"), config.report, ["pdf", "json"])
```

## Архитектура

```
             ┌──────────── core ────────────────────────────────────────┐
URL ──► discover_site (robots.txt, sitemap) ──► Crawler (async, httpx/HTTP2)
             │                                       │ каждая HTML-страница
             │                                       ▼
             │                         Check.check_page(page)   ← все плагины параллельно
             │                                       │
             │                  после обхода: Check.check_site() (агрегаты, пробы ресурсов, API)
             │                                       ▼
             │      FindingSink ──► report_builder (тексты, scoring) ──► AuditReport (pydantic)
             └───────────────────────────────────────────────────────────┘
                                                     │
                                  reporting: json · csv · html (Jinja2) · pdf (Chromium)
```

| Модуль | Назначение |
|---|---|
| `core/crawler.py` | Асинхронный обход: robots.txt, глубина, лимиты, цепочки редиректов, страницы из sitemap |
| `core/page.py` | Страница + ленивые извлечения (title, meta, ссылки, картинки, заголовки, JSON-LD, текст) |
| `core/check.py` | Базовый класс плагина: `setup` / `check_page` / `check_site`, `fail()` / `params()` |
| `core/rules.py` | Загрузка `rules.yaml`, слияние с оверрайдами конфига, локализация текстов |
| `core/probe.py` | Кэшируемые HEAD/GET-пробы ресурсов (битые ссылки, вес картинок) |
| `core/scoring.py` | Оценка разделов: вес важности × доля затронутых страниц |
| `core/events.py` | События прогресса (CLI сейчас, WebSocket/очередь в SaaS) |
| `checks/*` | Плагины проверок, каждый — папка с `__init__.py` и `rules.yaml` |
| `reporting/*` | Рендеры отчётов, UI-строки в `reporting/i18n/*.yaml` |

После проверки страницы её HTML освобождается, поэтому память не растёт линейно с размером сайта.
Агрегатам (дубли, ссылочный граф) хватает компактных данных.

## Изменение правил

Порог, важность, раздел и включённость любого правила меняются в конфиге:

```yaml
rules:
  images.too_large:
    params: { max_kb: 500 }      # было 200 — текст в отчёте тоже обновится: «более 500 КБ»
    severity: high
  meta.twitter_card_missing:
    enabled: false
```

Опечатка в id правила или в имени параметра сразу даст ошибку конфига, а не молчаливое игнорирование.

## Свой модуль проверок

Папка `my_checks/brand/` с двумя файлами:

```python
# __init__.py
from seoaudit.core.check import Check

class BrandCheck(Check):
    id = "brand"

    async def check_page(self, page):
        if self.params("brand_in_title")["brand"] not in (page.title or ""):
            self.fail("brand_in_title", page.url, title=page.title)
```

```yaml
# rules.yaml
category: meta
rules:
  brand_in_title:
    severity: low
    params: { brand: "ShopName" }
    text:
      ru:
        title: В title нет названия бренда
        description: ...
        recommendation: Добавьте «$brand» в конец title.
        message: "Title: «$title»"
        fix: |
          Markdown с примерами кода для разработчика
```

Подключение: `plugin_paths: [./my_checks]` в конфиге или entry point `seoaudit.checks` в pip-пакете.
Готовый пример лежит в [examples/plugins/analytics](examples/plugins/analytics).

В текстах доступны `$параметры` правила, в `message` — ещё и данные находки, переданные в `fail()`.
`message` может быть словарём вариантов: `fail(..., key="short")` выберет `message.short`.

## Что взято из open source

- **Протокол robots.txt:** `protego` (парсер Scrapy с поддержкой спецификации Google).
- **Microdata/RDFa:** `extruct`.
- **HTML-парсинг:** `selectolax` (Lexbor) — в десятки раз быстрее BeautifulSoup.
- **Безопасный XML для sitemap:** `defusedxml`.
- **Lighthouse и CrUX:** PageSpeed Insights API или локальный Lighthouse (Node.js) на Chromium от Playwright.
- **Валидация HTML:** W3C Nu Validator (можно поднять локально через Docker).
- **Идеи проверок:** SEOnaut, SiteOne Crawler, Screaming Frog, Lighthouse SEO, OWASP Secure Headers.

## Дорожная карта к SaaS

Фундамент уже заложен:
- `Auditor` — библиотека без глобального состояния.
- Результат — сериализуемая pydantic-модель.
- Есть события прогресса и рендер отчёта из сохранённого JSON.
- Конфиг можно задать для каждого клиента.

Следующие шаги:
1. FastAPI + очередь (arq/Celery): `POST /audits` создаёт задачу, worker вызывает `Auditor.run()`,
   события уходят в Redis pub/sub → WebSocket.
2. Хранилище: PostgreSQL (`AuditReport` в JSONB), PDF в S3.
3. Мультитенантность: конфиг правил на организацию (white-label через `report.meta`).
4. Рендеринг JS-сайтов (SPA) через Playwright как альтернативный fetcher краулера.
5. Сравнение аудитов во времени (diff находок по `rule_id` + `url`), мониторинг по расписанию.

## Тесты

```bash
pytest
```

E2E-тест гоняет полный аудит офлайн на мок-сайте (`httpx.MockTransport`).

## Лицензия

[MIT](LICENSE)
