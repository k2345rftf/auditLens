# Анализ проекта AuditLens (модуль `loophole`)

**Дата анализа:** 2026-07-27
**Сфокусирован на:** модуль `bank_audit.loophole`
**Уверенность оценки:** 🟡 средняя — анализ основан на чтении документации, кода, истории git, pytest/ruff/mypy. Для бизнес-метрик и production-наблюдений потребуются дополнительные источники.

---

## 1. Текущее состояние проекта

### 1.1. Общая характеристика

AuditLens — Deep-Research платформа для внутреннего аудита розничных банковских продуктов. Ядро на Python 3.11+/FastAPI, фронт на React 18 без бандлера (JSX + Babel в браузере), БД PostgreSQL 17.5 + pgvector в продакшене, SQLite — в тестах.

Фокус текущей активности — модуль `loophole` (поиск «лазеек» в продуктах и тарифах банков). В рабочем дереве идёт реализация фичи **«Полный контент источников»** (миграция 016, `content_fetch.py`, backfill, UI-разворачивание строк, CSV-экспорт). Спека утверждена 2026-07-26, план — `docs/superpowers/plans/2026-07-27-loophole-full-content.md`.

### 1.2. Последняя активность (git log с 2026-07-01)

Последние 20 коммитов показывают плотную работу вокруг генерируемых парсеров и UI:

- `80e8190` — feat(parsers.registry): delete parser directory recursively
- `8f51636` — style(healer.tests): reorder stdlib imports
- `0e0cdac` — feat(parsers.healer): reinstall requirements after code patch
- `8cb560b` — fix(parsers): keep parser ready after validation, detach background validation from request session
- `c1abc59` — test(web): update create_parser mock
- `7c74ad7` — feat(parsers): async validation loop on parser creation with rollback
- `90d3a5a` — fix(parsers.runner): release runner from registry regardless of finalize flag
- `239f6fe` — feat(parsers.runner): run parser from venv, parse JSON logs, read results.json
- `deffe55` — fix(parsers): use proc.communicate in install_requirements
- `994dbca` — refactor(parsers): align system prompt and docstring
- `ba4ca10` — feat(parsers): generate requirements.txt and save parser into isolated directory with venv
- `eb89d7c` — Доработка интерфейса по маркированию лазеек
- `869f3c4` — loophole: найденные в чате лазейки теперь попадают в таблицу

**Вывод:** команда в активной фазе стабилизации сложного подсистемного компонента (генерируемые парсеры + self-healing). Много правок связано с управлением сессиями, фоновыми задачами и изоляцией venv.

### 1.3. Состояние рабочего дерева

В рабочем дереве **31 изменённый файл** и **11 неотслеживаемых** (новые файлы миграций 015/016, `content_fetch.py`, тесты парсеров, UI-файлы). Ключевые изменённые области:

- `src/bank_audit/loophole/repository.py` — +315/-0 строк: поля контента, `update_content`, backfill-запросы
- `src/bank_audit/loophole/static/loophole.jsx` — +484/-143: UI разворачивания строк
- `src/bank_audit/loophole/chat/tools_nanobot.py` — +144: интеграция `content_fetch` в `save_loophole`
- `src/bank_audit/loophole/web.py` — +15: новые эндпоинты контента

Работа **не закоммичена** — соответствует правилу проекта «коммиты только по согласованию с пользователем».

### 1.4. Результаты тестов и статического анализа

| Проверка | Команда | Результат |
|---|---|---|
| pytest `tests/loophole` | `.venv/Scripts/python.exe -m pytest tests/loophole -q --tb=short` | **319 passed, 1 warning** (13.27s) |
| ruff `src/bank_audit/loophole` | `ruff check src/bank_audit/loophole --output-format=full` | **120 errors** |
| mypy `src/bank_audit/loophole` | `mypy src/bank_audit/loophole --ignore-missing-imports` | **38 errors** |

**Тесты зелёные** — функциональная корректность новой фичи подтверждена. При этом линтер и типизатор накапливают технический долг.

### 1.5. Структура модуля `loophole`

Модуль состоит из 37 исходных файлов `.py` и 4 поддиректорий:

- `chat/` — nanobot-агент, tools, clarify, hooks, prompts
- `kb/` — few-shot база знаний
- `parsers/` — генерация Scrapy-парсеров, runner, scheduler, healer, registry, dedup
- `adapters/` — обёртки над `rag.web_search` и `rag.fetcher`

Плюс статика: `loophole.jsx` (65 KB), `loophole.css` (32 KB), `loophole.html`.

---

## 2. Технический долг

Долг разделён на три уровня критичности. Источники: `docs/project-context.md`, результаты ruff/mypy, чтение кода.

### 2.1. Высокая критичность (блокирует стабильность в продакшене)

| # | Проблема | Где | Почему опасно | Источник |
|---|---|---|---|---|
| 1 | **Фоновые задачи пишут через закрытую/чужую сессию БД** | `parsers/healer.py`, `parsers/runner.py` | `asyncio.create_task(_heal_worker(..., session=None))` и `runner.wait()` после выхода из request-контекста работают с сессией, которая может быть закрыта. Это приводит к неприменённым коммитам, «висячим» run-записям и непредсказуемым ошибкам SQLAlchemy | project-context.md 2026-07-27 + код healer.py:104 |
| 2 | **B008: `Depends(get_session)` в аргументах по умолчанию** | `loophole/web.py` (9 эндпоинтов) | Стандартный антипаттерн FastAPI: сессия вычисляется один раз при импорте модуля, а не при каждом запросе. В тестах работает благодаря `dependency_overrides`, в продакшене может дать утечки/старые сессии | ruff B008 (25 срабатываний) |
| 3 | **Отсутствие типизации `session` и `workspace_id` в парсерах** | `parsers/generator.py`, `parsers/healer.py` | mypy находит 38 ошибок, в т.ч. `workspace_id: Any \| None` туда, где ожидается `int`; `row` может быть `None`, но код его индексирует без проверки | mypy + код |
| 4 | **Blind `except Exception` в критических путях** | `workspace.py:36`, `tools_nanobot.py`, `collector.py`, `parsers/*` | Подавляет все ошибки, усложняет отладку, может маскировать баги данных или БД | ruff BLE001 (49 срабатываний) |
| 5 | **Зависимость `selectolax` отсутствует в окружении для глобальных тестов** | `tests/loophole/test_static_bust.py` импортирует `bank_audit.web.app`, который тянет `rag.indexer` → `selectolax` | Тесты вне `.venv` падают с `ModuleNotFoundError`; сборка/CI на чистом окружении может ломаться | pytest collection error (без venv) |

### 2.2. Средняя критичность (замедляет разработку и снижает качество)

| # | Проблема | Где | Влияние |
|---|---|---|---|
| 6 | **120 ошибок ruff и 38 ошибок mypy** | весь `src/bank_audit/loophole` | Линтер не может использоваться как gate в CI. Новые ошибки смешиваются со старыми, регрессии незаметны. project-context.md уже фиксирует договорённость «нет НОВЫХ ошибок» вместо «ноль ошибок» |
| 7 | **Историческое несоответствие имён констант миграций** | `db_schema.py`: `MIGRATION_011_PATH` указывает на файл `013_loophole_agent.sql` | Путаница при добавлении миграций; требует ручного обновления `call_count` в нескольких тестах при каждой новой миграции | project-context.md 2026-07-26 |
| 8 | **Мёртвая заглушка `hook.records`** | `chat/hooks.py` | Таблица в UI наполняется через `audit_save_loophole` + ручной refresh, а не через хук. Технический артефакт, который не соответствует архитектуре | ARCHITECTURE_LOOPHOLE.md §8 |
| 9 | **Ручное управление `call_count` в тестах миграций** | `test_db_schema.py`, `test_db_schema_011.py`, `test_db_schema_014.py`, `test_db_schema_015.py`, `test_db_schema_016.py` | Хрупкие тесты: добавление каждой новой миграции требует правки N файлов | project-context.md 2026-07-26 + код |
| 10 | **Жёсткая связь с `nanobot-ai` и Gemini-совместимостью через runtime-патч** | `chat/nanobot_agent.py` `_patch_registry_for_gemini` | Патч схлопывает `type: ["string", "null"]` в `type: "string"`. Это лечит симптом, но не причину nullable-схем инструментов; риск регрессий при обновлении nanobot-ai | project-context.md 2026-07-27 |
| 11 | **Большой JSX без бандлера (65 KB)** | `static/loophole.jsx` | Сложно рефакторить, нет tree-shaking, ошибки Babel видны только в браузере, размер растёт | структура файлов |

### 2.3. Низкая критичность (пластырь/документация)

| # | Проблема | Где | Влияние |
|---|---|---|---|
| 12 | **Предсуществующие сломанные тесты `test_smoke.py` и `tests/test_digest.py`** | глобальные тесты | Отключены из регрессионного прогона; риск скрытых регрессий в `sources/sravni_aggregator.py` и других модулях | project-context.md 2026-07-26/27 |
| 13 | **Ручной CSS cache-bust только частично решён** | `web/app.py` | Ранее `loophole.css` кэшировался эвриститически; добавлен `?v=mtime`, но после деплоя сервер без `--reload` требует перезапуска | project-context.md 2026-07-26 |
| 14 | **Кросс-импорты между тестами модуля** | `tests/loophole/*` | Повышают связность тестов, усложняют параллельный запуск | наблюдение по структуре |

---

## 3. Потенциальные улучшения

### 3.1. Немедленные (до merge текущей ветки)

| Приоритет | Улучшение | Что сделать | Критерий успеха |
|---|---|---|---|
| P0 | **Исправить передачу сессий в фоновые задачи** | В `healer.py` и `runner.py` фоновые задачи должны открывать свою сессию через `_session(None)`/`db.session()`, а не использовать request-сессию. request-сессию оставить только для синхронной части | `pytest tests/loophole/test_parsers_healer.py tests/loophole/test_parsers_runner.py -q` зелёный; ручная проверка, что run финализируется после HTTP-ответа |
| P0 | **Убрать `Depends` из default-аргументов** | Заменить `session=Depends(get_session)` на `Annotated[Session, Depends(get_session)]` или перенести вызов в тело функции | ruff B008 = 0 |
| P1 | **Покрыть типизацию критических путей** | Добавить type hints в `generator.py`, `healer.py`, `scheduler.py`, устранить 38 mypy-ошибок | `mypy src/bank_audit/loophole --ignore-missing-imports` без ошибок |
| P1 | **Устранить blind `except Exception`** | В `workspace.py` заменить на конкретные исключения; в остальных местах добавить логирование stack trace и re-raise, если это не ожидаемый fallback | BLE001 уменьшить хотя бы на 50% |

### 3.2. Ближайшие (следующий спринт)

| Приоритет | Улучшение | Что сделать | Ожидаемый эффект |
|---|---|---|---|
| P2 | **Автоматизировать `call_count` в тестах миграций** | `apply_migration` возвращает список применённых миграций; тесты проверяют наличие ожидаемых SQL-фрагментов, а не точное число | Добавление миграции 017 потребует правки ≤2 файлов |
| P2 | **Ввести CI-gate: ruff + pytest на `.venv`** | GitHub Actions / локальный хук: устанавливать зависимости из `pyproject.toml`, запускать `ruff` и `pytest tests/loophole` | Новые ошибки не попадают в main |
| P2 | **Удалить или оживить `hook.records`** | Либо реализовать push найденных лазеек из чата в таблицу через хук, либо удалить мёртвый код | Меньше путаницы в архитектуре |
| P2 | **Добавить регрессионные тесты для `test_smoke.py` и `test_digest.py`** | Исправить bytes/encoding баги (уже частично сделано 2026-07-27) и вернуть в прогон | Общее покрытие растёт, регрессии в парсерах ловятся раньше |
| P2 | **Вынести `nanobot-ai` Gemini-патч в конфигурацию схем** | Вместо runtime `_collapse_type_arrays` сделать так, чтобы инструменты сами генерировали Gemini-совместимые схемы | Меньше магии, легче обновлять nanobot-ai |

### 3.3. Стратегические (дорожная карта)

| Приоритет | Улучшение | Обоснование |
|---|---|---|
| P3 | **Разделить `loophole.jsx` на модули + ввести сборщик (Vite/esbuild)** | Сейчас 65 KB JSX без бандлера — это потолок масштабируемости. Сборщик даст HMR, tree-shaking, типизацию (TypeScript), автотесты компонентов |
| P3 | **Ввести ORM/генерацию SQL по схеме или хотя бы schema-first миграции** | Ручной `sqlalchemy.text` в 38+ функциях `repository.py` приводит к расхождению моделей, миграций и тестового `conftest.py`. Автогенерация DDL из Pydantic-схем снизит ошибки |
| P3 | **Асинхронная очередь для backfill и heal** | Сейчас `POST /records/backfill-content` синхронный и блокирует запрос. Для тысяч legacy-записей нужен фоновый worker (celery/arq/собственный) с мониторингом |
| P3 | **Полнотекстовый поиск по контенту** | Сейчас `query_text` ищет через `LIKE` по `title/snippet/raw_text`. При росте таблицы `loophole_record` это станет узким местом. Greenplum/PostgreSQL FTS или отдельный индекс решат проблему |
| P3 | **Маскировка ПДн в хранимом контенте как опция** | Сейчас маскировка только перед LLM. Для compliance может понадобиться опциональное обезличивание хранимого `raw_text` |

---

## 4. Метрики и факты

| Метрика | Значение |
|---|---|
| Всего `.py` файлов в `src/bank_audit/loophole` | 37 |
| Строк кода в `loophole.jsx` | ~65 000 |
| Строк кода в `loophole.css` | ~32 000 |
| Тестов в `tests/loophole` | 319 шт. (все пройдены) |
| Ошибок ruff в `src/bank_audit/loophole` | 120 (24 автоисправимых) |
| Ошибок mypy в `src/bank_audit/loophole` | 38 |
| Новых миграций loophole (не в HEAD) | 2 (015, 016) |
| Новых тестовых файлов loophole (не в HEAD) | 7 |

---

## 5. Вывод

Модуль `loophole` находится в активной фазе развития: функциональность «полный контент источников» реализована и покрыта тестами (319 passed), но **инфраструктура качества отстаёт от скорости разработки**. Главные риски — управление сессиями БД в фоновых задачах и антипаттерн `Depends` в аргументах по умолчанию; они могут дать нестабильность в продакшене, которую юнит-тесты на SQLite не поймают.

Рекомендуемый порядок действий:

1. **До merge** — исправить сессии в `healer/runner` и `Depends` в `web.py`.
2. **Сразу после merge** — добить mypy/ruff до нуля и поставить CI-gate.
3. **Следующий спринт** — автоматизировать тесты миграций, вернуть `test_smoke.py`/`test_digest.py` в прогон, убрать мёртвый `hook.records`.
4. **Квартал** — рассмотреть сборщик для JSX и асинхронную очередь для backfill.

---

*Документ сгенерирован агентом Mary (Business Analyst). Данные верифицированы запуском pytest/ruff/mypy на рабочем дереве 2026-07-27.*
