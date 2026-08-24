# Эпики и истории: рефакторинг и развитие `bank_audit.loophole`

**ADR:** `docs/adr/ADR-001-loophole-refactoring-plan.md`
**Дата:** 2026-07-27
**Автор:** Winston (System Architect)
**Статус:** Proposed

---

## Эпик 1. Стабилизация продакшена (P0)

**Цель:** убрать риски, которые дадут нестабильность в проде, но не ловятся юнит-тестами на SQLite.

---

### История 1.1 — Фоновые задачи парсеров не используют request-сессию

**Описание:**
Сейчас `runner.py` создаёт `asyncio.create_task(runner.wait())`, а `healer.py` — `asyncio.create_task(_heal_worker(..., session=None))`. Если эти задачи выполняются после возврата HTTP-ответа, request-сессия может быть уже закрыта. Это приводит к "висячим" run-записям и неприменённым коммитам.

**Затронутый код:**
- `src/bank_audit/loophole/parsers/runner.py:151`
- `src/bank_audit/loophole/parsers/healer.py:104`

**Задачи:**
1. В `ParserRunner.__init__` убрать хранение внешней `session`.
2. В `wait()` открывать `db.session()` в начале и закрывать в `finally`.
3. В `_heal_worker` открывать `db.session()` локально для каждой фазы (чтение, запись).
4. Убедиться, что синхронная часть `run()` всё ещё использует request-сессию для `create_run`.

**Критерии приёмки:**
- `pytest tests/loophole/test_parsers_healer.py tests/loophole/test_parsers_runner.py -q` — зелёный.
- Ручная проверка: run финализируется после HTTP-ответа при отключенной request-сессии.

**Сложность:** S
**Владелец:** backend
**Зависимости:** нет

---

### История 1.2 — Убрать `Depends(get_session)` из default-аргументов

**Описание:**
9 эндпоинтов в `web.py` используют `session=Depends(get_session)`. Это стандартный антипаттерн FastAPI: dependency вычисляется один раз при импорте модуля, а не при каждом запросе.

**Затронутый код:**
- `src/bank_audit/loophole/web.py` (множественные эндпоинты)

**Задачи:**
1. Заменить на `Annotated[Session, Depends(get_session)]` или перенести получение сессии в тело функции.
2. Обновить тесты, использующие `dependency_overrides`, если требуется.

**Критерии приёмки:**
- `ruff check src/bank_audit/loophole --select B008` = 0.
- Все тесты `tests/loophole/test_web.py` зелёные.

**Сложность:** S
**Владелец:** backend
**Зависимости:** нет

---

### История 1.3 — Заменить blind `except Exception` на конкретные исключения

**Описание:**
В `workspace.py`, `collector.py`, `tools_nanobot.py`, `parsers/*` встречаются `except Exception`, которые подавляют все ошибки и усложняют отладку.

**Задачи:**
1. В `workspace.py` заменить на конкретные исключения ОС/IO.
2. Во всех остальных местах добавить `log.exception(...)` и re-raise, если это не ожидаемый fallback.
3. Оставить широкий `except` только в точках graceful fallback (например, SSE-стрим, фоновые worker'ы).

**Критерии приёмки:**
- `ruff --select BLE001 src/bank_audit/loophole` уменьшился минимум на 50% (сейчас 49 срабатываний).
- Все тесты `tests/loophole` зелёные.

**Сложность:** M
**Владелец:** backend
**Зависимости:** нет

---

## Эпик 2. Инфраструктура качества (P1)

**Цель:** сделать линтер и тесты gate'ом, убрать хрупкие тесты.

---

### История 2.1 — Исправить mypy-ошибки в критических путях

**Описание:**
Сейчас `mypy src/bank_audit/loophole --ignore-missing-imports` показывает 38 ошибок. Большинство в `generator.py`, `healer.py`, `scheduler.py`: `workspace_id: Any | None` там, где ожидается `int`; `row` индексируется без проверки на `None`.

**Задачи:**
1. Добавить `from __future__ import annotations` и type hints в `parsers/generator.py`.
2. Проверять `row` на `None` перед индексацией в `healer.py`.
3. Уточнить типы параметров `session` (использовать общий протокол/тип из `bank_audit.db`).
4. Добавить `# type: ignore` только для нетипизированных сторонних библиотек.

**Критерии приёмки:**
- `mypy src/bank_audit/loophole --ignore-missing-imports` = 0 ошибок.

**Сложность:** M
**Владелец:** backend
**Зависимости:** 1.3

---

### История 2.2 — Настроить CI gate: ruff + pytest

**Описание:**
Новые ошибки линтера и падающие тесты попадают в код, потому что нет автоматической проверки в PR.

**Задачи:**
1. Добавить GitHub Actions workflow `.github/workflows/loophole-ci.yml`.
2. Workflow должен:
   - устанавливать зависимости из `pyproject.toml`;
   - запускать `ruff check src/bank_audit/loophole`;
   - запускать `pytest tests/loophole -q`.
3. Настроить branch protection: без зелёного CI merge запрещён.

**Критерии приёмки:**
- Любой PR с новыми ruff/pytest-ошибками в `loophole` нельзя смержить.
- CI проходит на текущем рабочем дереве.

**Сложность:** S
**Владелец:** DevOps/backend
**Зависимости:** 2.1

---

### История 2.3 — Автоматизировать `call_count` в тестах миграций

**Описание:**
При добавлении каждой новой миграции приходится обновлять `call_count` в нескольких тестовых файлах (`test_db_schema.py`, `test_db_schema_011.py`, `test_db_schema_014.py` и др.). Это хрупко.

**Задачи:**
1. Изменить `db_schema.apply_migration` так, чтобы он возвращал список применённых миграций.
2. Переписать тесты: проверять наличие ожидаемых SQL-фрагментов в применённых миграциях, а не точное число.
3. Убрать или переименовать историческое несоответствие `MIGRATION_011_PATH` → файл `013_loophole_agent.sql`.

**Критерии приёмки:**
- Добавление миграции 017 требует правки ≤2 тестовых файлов.
- Все `test_db_schema_*.py` зелёные.

**Сложность:** M
**Владелец:** backend
**Зависимости:** нет

---

### История 2.4 — Вернуть/исправить `test_smoke.py` и `test_digest.py`

**Описание:**
Тесты сломаны с initial commit из-за `bytes`-литерала с не-ASCII и чтения фикстуры без `encoding` на Windows. Они исключены из регрессионного прогона.

**Задачи:**
1. Исправить `bytes` → `encode("utf-8")` в `tests/test_smoke.py`.
2. Добавить `encoding="utf-8"` при чтении фикстур в `tests/test_digest.py`.
3. Вернуть тесты в общий прогон (например, через `pytest tests/test_smoke.py tests/test_digest.py`).

**Критерии приёмки:**
- `pytest tests/test_smoke.py tests/test_digest.py -q` — зелёный.

**Сложность:** S
**Владелец:** backend
**Зависимости:** нет

---

## Эпик 3. Безопасность и изоляция (P2)

**Цель:** LLM и сгенерированный код не могут навредить; SQL остаётся read-only.

---

### История 3.1 — Human-in-the-loop для healer patch

**Описание:**
Сейчас `patch_parser` в `tools_nanobot.py` атомарно заменяет файл `parser.py`. Автоматический heal может применить патч без контроля.

**Затронутый код:**
- `src/bank_audit/loophole/chat/tools_nanobot.py:297-314`
- `src/bank_audit/loophole/parsers/healer.py`

**Задачи:**
1. В `heal_tick` и `_heal_worker` отключить автоматическое применение патча.
2. Healer формирует `patch_proposal` с полями: `diff`, `reason`, `risk_level`.
3. Добавить API:
   - `GET /parsers/{parser_id}/pending-patch` — список предложенных патчей;
   - `POST /parsers/{parser_id}/pending-patch/{patch_id}/apply` — применить патч;
   - `POST /parsers/{parser_id}/pending-patch/{patch_id}/reject` — отклонить.
4. Все применения/отклонения логировать в `loophole_action_log`.

**Критерии приёмки:**
- Автоматический heal не пишет в файлы без `manual_approve=True`.
- В UI/тестах можно просмотреть diff и принять/отклонить патч.
- Все тесты `test_parsers_healer.py` зелёные.

**Сложность:** L
**Владелец:** backend + frontend
**Зависимости:** 1.1

---

### История 3.2 — Усилить SQL guard AST-парсингом

**Описание:**
Текущий `_is_read_only_select` использует blacklist-регулярки, которые можно обойти через комментарии, строковые литералы или CTE.

**Затронутый код:**
- `src/bank_audit/loophole/chat/tools_nanobot.py:30-44`

**Задачи:**
1. Добавить зависимость `sqlparse`.
2. Реализовать `_is_read_only_select_ast(sql)`:
   - корневой оператор — `SELECT`;
   - отсутствуют DML/DDL в CTE и подзапросах;
   - присутствует `LIMIT` (или добавлять автоматически).
3. Добавить регрессионные тесты на обходы: комментарии, строковые литералы, `UNION`, CTE с `INSERT`.

**Критерии приёмки:**
- Корректные SELECT работают.
- Все попытки обхода отклоняются.
- `pytest tests/loophole/test_db_query_tool.py` зелёный.

**Сложность:** M
**Владелец:** backend
**Зависимости:** нет

---

### История 3.3 — Sandbox для сгенерированных парсеров

**Описание:**
Сгенерированный `parser.py` запускается в venv, но без ограничений на сеть и ФС. LLM может теоретически сгенерировать вредоносный код.

**Задачи:**
1. Ограничить network egress парсера только разрешёнными доменами (через `firejail`/`nsjail` или контейнер).
2. Запретить запись вне `parsers/catalog/parser_<id>_*`.
3. Добавить лимиты CPU/памяти на subprocess.
4. Добавить тест: парсер не может записать файл за пределами своей директории.

**Критерии приёмки:**
- Парсер пишет только внутри `parsers/catalog/parser_<id>_*`.
- Парсер не может открыть соединение на произвольный хост.
- Тест sandbox проходит.

**Сложность:** L
**Владелец:** backend + DevOps
**Зависимости:** 3.1

---

## Эпик 4. Масштабируемость и архитектура (P3)

**Цель:** модуль растёт без боли — разделение ответственности, фоновые задачи, поиск.

---

### История 4.1 — Разбить `web.py` на подроутеры

**Описание:**
`web.py` содержит 812 строк и смешивает records, chat, parsers, export, admin. Это затрудняет тестирование и ревью.

**Задачи:**
1. Создать директорию `src/bank_audit/loophole/routers/`.
2. Выделить:
   - `records.py` (search, records, verdict, backfill, content)
   - `chat.py` (chat, clarify, history)
   - `parsers.py` (catalog, run, stop, status, heal)
   - `export.py` (export json/csv/pdf)
   - `admin.py` (keywords, collect, refine, workspaces)
3. `loophole/web.py` остаётся точкой монтирования всех роутеров.

**Критерии приёмки:**
- Ни один роутер не превышает 250 строк.
- Все тесты `tests/loophole/test_web.py` и `tests/loophole/test_parsers_web.py` зелёные.

**Сложность:** M
**Владелец:** backend
**Зависимости:** 1.2, 2.2

---

### История 4.2 — Асинхронная очередь для backfill

**Описание:**
`POST /records/backfill-content` синхронно обрабатывает записи и использует `time.sleep`, блокируя HTTP-worker.

**Затронутый код:**
- `src/bank_audit/loophole/web.py:140-183`

**Задачи:**
1. Внедрить очередь (ARQ или Celery, см. ADR).
2. `POST /records/backfill-content` ставит задачу и возвращает `job_id`.
3. Добавить `GET /records/backfill-content/status/{job_id}`.
4. Worker выполняет backfill порциями с `delay_ms` паузой.

**Критерии приёмки:**
- Backfill для 1000 записей не блокирует HTTP-worker.
- Статус задачи можно получить по `job_id`.
- Тесты на backfill зелёные.

**Сложность:** L
**Владелец:** backend + DevOps
**Зависимости:** 4.1

---

### История 4.3 — Batch dedup и bulk insert в runner

**Описание:**
В `runner.py` каждый результат парсера проверяется тремя отдельными `SELECT`.

**Затронутый код:**
- `src/bank_audit/loophole/parsers/runner.py:347-378`

**Задачи:**
1. Добавить `repo.exists_many(shas, urls, text_shas)` — один SELECT для всех ключей.
2. Добавить `repo.insert_many(records)` — batch INSERT.
3. В `runner.py` группировать результаты и выполнять dedup/insert пакетами.

**Критерии приёмки:**
- Runner обрабатывает 1000 результатов за ≤3 SQL-запроса.
- Тесты на dedup (`test_parsers_runner.py`) зелёные.
- Нет регрессий в `test_parsers_dedup.py`.

**Сложность:** M
**Владелец:** backend
**Зависимости:** 1.1

---

### История 4.4 — Full-text search вместо LIKE

**Описание:**
`search_relevant` и `list_records` используют `LIKE '%q%'` по `title/snippet/raw_text`. При росте таблицы это станет узким местом.

**Затронутый код:**
- `src/bank_audit/loophole/repository.py:268-289`, `305-347`

**Задачи:**
1. Добавить миграцию с `tsvector` для Greenplum/PostgreSQL (или отдельный FTS-индекс).
2. Обновить `search_relevant` и `list_records` для использования FTS.
3. Оставить fallback на LIKE для SQLite-тестов.
4. Добавить benchmark: поиск по 100K записей <500 мс.

**Критерии приёмки:**
- Поиск по 100K записей выполняется <500 мс на Greenplum.
- SQLite-тесты продолжают проходить.

**Сложность:** L
**Владелец:** backend + DBA
**Зависимости:** 2.3

---

### История 4.5 — Разделить `repository.py` по доменам

**Описание:**
`repository.py` содержит 950+ строк и смешивает CRUD для records, parsers, workspace, chat, KB, audit.

**Задачи:**
1. Создать `src/bank_audit/loophole/repositories/`:
   - `records.py`
   - `parsers.py`
   - `workspaces.py`
   - `chat.py`
   - `kb.py`
   - `audit.py`
2. Сохранить обратную совместимость через `repository.py` как агрегирующий модуль.
3. Постепенно переключать вызывающий код на новые модули.

**Критерии приёмки:**
- Ни один файл репозитория не превышает 300 строк.
- Все тесты `tests/loophole` зелёные.

**Сложность:** L
**Владелец:** backend
**Зависимости:** 4.1

---

## Дорожная карта

| Неделя | Эпик | Истории | Цель |
|---|---|---|---|
| 1 | 1 | 1.1, 1.2 | Убрать блокеры стабильности |
| 2 | 1, 2 | 1.3, 2.1, 2.2 | Поднять качество кода и CI |
| 3 | 2, 3 | 2.3, 2.4, 3.2 | Хрупкие тесты + безопасность SQL |
| 4–5 | 3 | 3.1, 3.3 | Human-in-the-loop heal + sandbox |
| 6–7 | 4 | 4.1, 4.3 | Разделение API и batch dedup |
| 8–10 | 4 | 4.2, 4.4, 4.5 | Очередь, FTS, разделение repository |

---

## Примечания

- Каждая история — отдельный PR.
- Перед стартом Эпика 4.2 требуется уточнить выбор очереди (ARQ vs Celery) и наличие Redis в инфраструктуре.
- После принятия ADR необходимо обновить `docs/ARCHITECTURE_LOOPHOLE.md` и `.cursor/plan_research/MAP.md`.
