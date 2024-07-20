# seoaudit

Модульный SEO-аудитор сайтов: краулер + плагины проверок + отчёты с рекомендациями.

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Использование

```bash
seoaudit audit https://example.com          # PDF + JSON в ./reports
seoaudit audit example.com --max-pages 500
seoaudit rules                               # все правила с текущими порогами
seoaudit checks                              # подключённые модули
```

Пороги, важность и тексты правил лежат в `rules.yaml` рядом с модулем проверки.
