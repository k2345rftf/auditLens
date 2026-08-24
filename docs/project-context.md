# Контекст проекта: проблемы и решения

Формат: [ДАТА] Проблема: X → Решение: Y

[2026-07-26] Проблема: SQLite не понимает PostgreSQL-каст `:emb::vector` в `save_kb_example` — INSERT падал бы в SQLite-тестах → Решение: при `embedding=None` INSERT идёт без колонки embedding (две ветки SQL); в тестах `embedder.embed_one` мокается на сбой → срабатывает graceful fallback в `kb.add_example` (warning + embedding=None).

[2026-07-27] Проблема: POST /records/verdict при is_loophole=true (добавление в KB) → `psycopg.errors.SyntaxError: syntax error at or near ":"` на `:emb::vector` — SQLAlchemy text() парсит bind как `em`, литерал `:emb::vector` уходит в PostgreSQL → Решение: заменить `:emb::vector` на `CAST(:emb AS vector)` в `save_kb_example`, `search_kb_similar`, `kb.add_doc` (как в rag/indexer.py); регрессия `test_save_kb_example_with_embedding_uses_cast_bind`.

[2026-07-26] Проблема: `ruff check` по модулю loophole показывает 49+ предсуществующих ошибок (F401 и др.) — критерий «линтер без ошибок» недостижим → Решение: критерий переформулирован как «нет НОВЫХ ошибок»: сравнение worktree vs HEAD по затронутым файлам (15 vs 16 — стало на 1 меньше).

[2026-07-26] Проблема: `tests/test_smoke.py` — SyntaxError (bytes-литерал с не-ASCII) и `tests/test_digest.py::test_tg_parses_real_fixture` — UnicodeDecodeError (Windows, чтение фикстуры без encoding); оба предсуществующие на чистом HEAD → Решение: исключены из регрессионного прогона, вне скоупа фичи; не чинились.

[2026-07-26] Проблема: тесты `apply_migration` в трёх файлах жёстко проверяли `call_count == 2` — добавление миграции 014 их сломало → Решение: при добавлении новой миграции обновлять `call_count` в `test_db_schema.py`, `test_db_schema_011.py` и тесте новой миграции; имена констант (MIGRATION_011_PATH → файл 013) исторически расходятся с номерами файлов.

[2026-07-26] Проблема: ветка main содержала чужие незакоммиченные изменения в файлах плана (web.py, loophole.jsx, loophole.css, test_db_schema_011.py) → Решение: работа велась в новой ветке `feat/manual-verdict-marking` поверх рабочего дерева (`git switch -c` сохраняет изменения); hunks задач отделялись от чужих при ревью; коммиты не выполняются без подтверждения пользователя.

[2026-07-26] Проблема: фронт loophole.jsx нельзя проверить визуально без поднятого бэкенда и БД; toast маркировки (4 сек) исчезал до скриншота Playwright → Решение: стенд в `%TEMP%\kilo\lp-stand`: копии loophole.css/jsx + index.html с моком `window.fetch` на `/api/loophole/*` (workspace/banks/records/verdict), `python -m http.server`; Babel-ошибки видны в консоли браузера; для съёмки toast `window.setTimeout` глушится для ms===4000.

[2026-07-26] Проблема: «не подгрузились css стили для маркирования лазеек» — браузер кэшировал `loophole.css` эвристически (Starlette StaticFiles не шлёт Cache-Control), а `_loophole_html_with_bust()` версионировал только jsx; новый JSX + старый CSS = разметка без стилей → Решение: bust `?v=mtime` добавлен и для css (app.py); регрессионный тест `tests/loophole/test_static_bust.py`; импорт `bank_audit.web.app` в тесте требует `DATABASE_URL=sqlite:///:memory:` (conftest ставит sqlite+aiosqlite, но aiosqlite не установлен). После деплоя правки сервер без --reload нужно перезапустить.

[2026-07-27] Проблема: `registry.delete_parser` (Task 1) падал с `cannot import name 'db' from 'bank_audit.loophole'` — относительный импорт `from .. import db` внутри `parsers/` указывает на `bank_audit.loophole`, а `db` лежит уровнем выше → Решение: исправлено на `from ... import db` (как в `parsers/scheduler.py`); всплыло только в Task 10, т.к. раньше путь default-сессии не покрывался тестом с реальным delete.

[2026-07-27] Проблема: runner.run(session=<request>) — фоновая wait() пишет через закрытую сессию, записи не коммитятся → Решение: фонозапускающие вызовы (runner.run) всегда без session; request-session только для синхронной части запроса

[2026-07-27] Проблема: незащищённый reap_stale_runs в lifespan ронял старт приложения при недоступной БД → Решение: best-effort инициализация в lifespan оборачивается в try/except с log.warning

[2026-07-27] Проблема: tests/test_smoke.py (SyntaxError non-ASCII bytes) и tests/test_digest.py (UnicodeDecodeError на Windows) были сломаны с initial commit → Решение: bytes→encode("utf-8") / явный encoding="utf-8" при чтении фикстур

[2026-07-27] Проблема: после починки SyntaxError test_smoke выявил 2 реальных бага парсера sravni_aggregator: selectolax `css()` дублирует ноду, совпадающую с несколькими селекторами группы (карточки yield'ились дважды), и "от 6 до 12 мес" парсился как (12, 12) → Решение: дедупликация по `node.mem_id` в parse_offers; `_PERIOD_RANGE_RE` ("от X до Y мес") проверяется до поединичного `_PERIOD_RE` в `_extract_term_months`.

[2026-07-27] Проблема: healer падал с Gemini 400 `Unknown name "type" ... Proto field is not repeating` — встроенные tools nanobot (complete_goal, long_task) регистрируются всегда (enabled()=True при наличии sessions, конфиг их не отключает) и генерируют nullable-схемы `"type": ["string","null"]`, а Gemini через OpenAI-совместимый эндпоинт не принимает type-массив → Решение: `_patch_registry_for_gemini` в `create_nanobot` оборачивает `registry.get_definitions` и рекурсивно схлопывает type-массивы в одиночный тип (`_collapse_type_arrays`); регрессионный тест `test_create_nanobot_tool_schemas_gemini_compatible`.

[2026-07-27] Проблема: фикстура `_mock_venv_and_pip` мокировала `install_requirements` синхронной `lambda`, но `generate_parser` вызывает `await install_requirements(...)` → Решение: заменить на `async def fake_install_requirements(*a, **kw): return None`.

[2026-07-27] Проблема: в тесте `test_wait_finalize_false_does_not_finish_run` из Task 03 использовался `run_id=777`, но `repo.get_run(777)` возвращал `None` — `ParserRunner` не создаёт запись при переданном `run_id`, а БД не содержит такого id → Решение: перед созданием `ParserRunner` явно создать run через `repo.create_run` и передать полученный `run_id`.

[2026-07-27] Проблема: после добавления reinstall в healer тесты worker'а стали вызывать реальный pip install (FileNotFoundError) и новый тест через heal() запускал фоновый воркер без db_cm → aiosqlite не найден → Решение: в тестах успешного/падучего патча замокан `healer.generator_mod.install_requirements`; в `test_heal_reinstalls_requirements` добавлена фикстура `db_cm`, импорт Path/asyncio, фейк install сделан async; ожидание фонового воркера через `await asyncio.sleep(0)`.

[2026-07-27] Проблема: `_CHARSET_RE` в fetch_decorator содержал `[A-Za-z0-9_\\-]` — в raw byte string это «бэкслеш ИЛИ дефис», charset с бэкслешем (`utf\-8`) ошибочно матчился целиком → Решение: заменено на `[A-Za-z0-9_-]` (дефис в конце класса — литерал); проверено скриптом: `windows-1251` парсится, `utf\-8` больше не матчится бэкслеш.
