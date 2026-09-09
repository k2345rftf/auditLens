---
title: 'Сохранение мошеннических схем в каталог с classification fraud_scheme'
type: 'feature'
created: '2026-09-09'
status: 'done'
baseline_commit: '27c9f4381822c85ed513760ced7e36f9a3d45b07'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
deferred:
  - summary: >-
      Дедуп автоимпорта по URL теряет вторую title-группу источника: источник с
      находками обоих типов даёт одну запись (fraud приоритетен), лазейка с того же
      URL не фиксируется отдельной записью; порядок title-групп без ORDER BY
      недетерминирован (candidate_title/confidence могут различаться между прогонами).
    evidence: |-
      research_cases.py: import_preliminary_sources группирует по candidate.title,
      сортирует только ORDER BY source.source_id, repo.exists_url() пропускает
      повторный URL. Пре-существующая семантика дедупа, обострённая fraud-приоритетом.
    location: >-
      src/bank_audit/loophole/research_cases.py (import_preliminary_sources)
    severity: low
  - summary: >-
      Латентное взаимодействие: classify_candidates классифицирует кандидатов
      лазейко-ориентированным классификатором без учёта finding_type; если путь
      когда-нибудь подключат к fraud-кандидатам, model_is_loophole=False через
      COALESCE(model_is_loophole, is_loophole) снимет и вердикт, и fraud-тип при импорте.
    evidence: |-
      research_cases.py: classify_candidates (~365-451) не фильтрует finding_type;
      маппинг импорта использует COALESCE(model_is_loophole, is_loophole).
      Путь сейчас не подключён к chat runtime (см. deferred-work.md).
    location: >-
      src/bank_audit/loophole/research_cases.py (classify_candidates)
    severity: low
---

<intent-contract>

## Intent

**Problem:** Найденные агентом «Лазейки» мошеннические схемы не сохраняются в БД: промпты 04/07 запрещают передавать мошеннические материалы в извлечение и в подтверждённые находки (замороженное ограничение spec-widen-web-search, пересмотрено пользователем 2026-09-09: «Мошеннические схемы тоже должны сохраняться», «любая запись без зависимости от флага»). При этом схема БД уже поддерживает `classification='fraud_scheme'` (миграция 066, CHECK-констрейнт), каталог и UI его отображают — не хватает только пути доставки.

**Approach:** Расширить контракт извлечения полем `finding_type` ('loophole'|'fraud_scheme'): мошеннические материалы передаются в `audit_extract_loopholes`, извлекаются как положительные находки отдельного типа (`is_loophole=TRUE` по конвенции миграции 066 — «положительная находка любого типа»), сохраняются общим persist-путём и автоимпортируются в каталог с `classification='fraud_scheme'`. В тексте ответа мошенничество по-прежнему отдельный раздел и не называется лазейкой.

## Boundaries & Constraints

**Always:** Инвариант `is_loophole == (classification != 'not_confirmed')` (repository.update_verdict) и семантика миграции 066 не меняются. Находки без явного вердикта (`is_loophole=None`) по-прежнему отбрасываются. Валидация `eligible_findings` (цитата ⊂ текст источника, дата в периоде) действует и для fraud-находок. `is_loophole=FALSE` без fraud-признака → `not_confirmed` (поведение не меняется). Отдельный раздел «Мошеннические схемы» в ответе, безопасные цитаты, без пошаговых инструкций для совершения мошенничества и без ПД. Новая миграция — следующий номер 067, идемпотентная. Тесты без сети/БД.

**Block If:** Потребуется менять CHECK-констрейнт `classification`, ослаблять инвариант update_verdict или сохранять находки без вердикта.

**Never:** Не называть мошенническую схему «лазейкой» в тексте ответа и не смешивать разделы. Не выводить fraud-записи из-под фильтров периода публикации и дедупликации автоимпорта. Не создавать git-коммит.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Fraud-находка | extract вернул finding_type='fraud_scheme', is_loophole=true, цитата из прочитанного источника | persist в исследование + автоимпорт: запись каталога classification='fraud_scheme', is_loophole=TRUE, status='preliminary' | Невалидная цитата/период — отброс eligible_findings |
| Обычная лазейка | finding_type отсутствует/'loophole', is_loophole=true | classification='vulnerability' (как сейчас) | Не ожидается |
| Явная не-лазейка | is_loophole=false, без fraud-признака | classification='not_confirmed' (как сейчас) | Не ожидается |
| Источник с находками обоих типов | У источника кандидаты loophole и fraud | classification='fraud_scheme' (fraud приоритетнее при положительном вердикте) | Задокументировать в коде |
| Неизвестный finding_type | extract вернул мусор в поле | Нормализация к 'loophole' сервером | Не доверять значению модели |
| Находка без вердикта | is_loophole=None | Отброс (как сейчас) | — |

</intent-contract>

## Code Map

- `src/bank_audit/loophole/chat/prompt/04_extract_loopholes.md` — исключение мошенничества (строки про «не извлекать»/«преступления, а не лазейки»): разрешить извлечение fraud с полем finding_type; определение лазейки (не противоправное) сохранить.
- `src/bank_audit/loophole/chat/prompt/07_nanobot_system.md` — блок «Мошенничество и хищение — не лазейка и не кандидат каталога» и шаг 3 методологии: переписать — fraud передаётся в извлечение и сохраняется как fraud_scheme, в тексте остаётся отдельным разделом.
- `src/bank_audit/loophole/chat/tools_nanobot.py` — `extract_loopholes` (парсинг JSON-результата): добавить нормализованный `finding_type`; `_queue_confirmed_findings`: копировать его в pending_record.
- `migrations/067_loophole_candidate_finding_type.sql` — новая: `loophole_research_candidate.finding_type TEXT NOT NULL DEFAULT 'loophole'` + CHECK IN ('loophole','fraud_scheme'), идемпотентно.
- `src/bank_audit/loophole/research_cases.py` — `persist_managed_run` (запись кандидата): писать finding_type; `import_preliminary_sources`: джойн выбирает finding_type, classification = 'fraud_scheme' при fraud-кандидате с положительным вердиктом, иначе прежняя бинарная логика.
- `src/bank_audit/loophole/chat/graph.py` — `_public_finding`: добавить finding_type в проекцию для UI.
- `tests/loophole/test_tools_nanobot.py` — prompt-контракт :73-89 фиксирует запрет «не передавай мошеннические материалы» — переопределить под новый контракт.
- `tests/loophole/test_auto_catalog_import.py` — маппинг classification: добавить fraud→fraud_scheme, сохранить False→not_confirmed.
- `tests/loophole/test_nanobot_graph.py` — e2e-подобный тест: pending_record с finding_type='fraud_scheme' → запись каталога fraud_scheme.
- `tests/loophole/test_story_2_2_research_cases.py` (`_create_research_schema`) и `tests/loophole/test_story_2_5_submission_route.py` — тестовые SQLite-схемы: колонка finding_type у candidate (зеркалит миграции).
- `tests/loophole/test_record_classification_migration.py` — образец staging PG-теста миграции (skip без AUDITLENS_POSTGRES_STAGING_URL); для 067 добавлен такой же в `tests/loophole/test_candidate_finding_type_migration.py` плюс offline текстовый структурный тест.

## Tasks & Acceptance

**Execution:**
- `migrations/067_loophole_candidate_finding_type.sql` — новая колонка finding_type у кандидата с CHECK и дефолтом — носитель fraud-признака в persist-пути.
- `src/bank_audit/loophole/chat/tools_nanobot.py` — парсинг и нормализация finding_type в extract_loopholes, проброс в pending_record — доставка признака от модели к серверу.
- `src/bank_audit/loophole/research_cases.py` — запись finding_type кандидату и трёхзначный маппинг classification в import_preliminary_sources — сохранение в каталог.
- `src/bank_audit/loophole/chat/prompt/04_extract_loopholes.md` и `07_nanobot_system.md` — новый контракт: fraud извлекается с finding_type='fraud_scheme', сохраняется, в тексте — отдельный раздел без слова «лазейка».
- `src/bank_audit/loophole/chat/graph.py` — finding_type в _public_finding — карточки UI могут различать тип находки.
- `tests/loophole/test_story_2_2_research_cases.py` + перечисленные тесты — схема и контракты из I/O-матрицы.

**Acceptance Criteria:**
- Given прочитанный источник с мошеннической схемой, when извлечение вернуло finding_type='fraud_scheme' с валидной цитатой, then после завершения запуска в каталоге появляется запись classification='fraud_scheme', is_loophole=TRUE, status='preliminary'.
- Given находка is_loophole=true без fraud-признака, when выполняется автоимпорт, then classification='vulnerability'; given is_loophole=false без fraud-признака, then classification='not_confirmed'.
- Given модель вернула неизвестное значение finding_type, when сервер обрабатывает находку, then она трактуется как 'loophole'.
- Given текст ответа агента, when в нём есть мошенническая схема, then она представлена в отдельном разделе «Мошеннические схемы» и не названа лазейкой (prompt-контракт).

## Spec Change Log

## Review Triage Log

### 2026-09-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 12: (high 0, medium 6, low 6)
- defer: 2: (low 2)
- reject: 2
- addressed_findings:
  - `[medium]` `[patch]` Реклассификация затирала тип: classify.py передавал update_verdict без classification → fraud откатывался в vulnerability; исправлено (сохранение текущего classification при положительном вердикте), 2 теста.
  - `[medium]` `[patch]` finding_type не доходил до верификации ЦК КС (case_snapshot, get_candidate) и текстового отчёта — поле проброшено, candidate_report помечает «Тип: мошенническая схема», тесты.
  - `[medium]` `[patch]` UI-карточка fraud выглядела как лазейка — бейдж «мошенническая схема» в loophole.jsx по finding_type.
  - `[medium]` `[patch]` Миграция 067 проверялась только staging-skip-тестом, CHECK не добивался при частичном накате — DO-блок идемпотентности, offline текстовый структурный тест, комментарий о порядке деплоя (миграция до кода).
  - `[medium]` `[patch]` Хрупкая нормализация finding_type (регистр/разделители) — общий normalize_finding_type, тест «Fraud scheme».
  - `[medium]` `[patch]` KB-загрязнение: ручная маркировка клала fraud_scheme в базу знаний как пример лазейки — KB-пример только для vulnerability, переход типа удаляет пример, тест.
  - `[low]` `[patch]` Нет few-shot примера fraud в промпте 04 — добавлен.
  - `[low]` `[patch]` Шаг 6 промпта 07 без finding_type; статистика «только по лазейкам» подмешивала fraud — промпт учит фильтровать classification='vulnerability', колонка classification в схеме БД промпта.
  - `[low]` `[patch]` Ветка fraud + is_loophole=FALSE → not_confirmed задокументирована комментарием и тестом.
  - `[low]` `[patch]` Спека: устаревший пункт conftest.py, опечатка, неверное описание образца теста миграции, неполный план Verification — исправлено вне intent-contract.
  - deferred: дедуп импорта по URL теряет вторую title-группу + недетерминированный порядок групп; латентное взаимодействие classify_candidates с fraud-кандидатами (путь не подключён).
  - rejected: чтение «сохранять находки без вердикта (is_loophole=None)» — вердикт-гейт осознанная валидация доверия, уточнение владельца касалось семантики флага для fraud-записей; требование тестов через живого агента/LLM — вне конвенции проекта (тесты без сети и LLM).

## Design Notes

is_loophole=TRUE для fraud — не «объявление мошенничества лазейкой», а конвенция миграции 066: флаг означает положительную находку любого типа, тип несёт classification. Это сохраняет инвариант update_verdict и включает fraud-записи в confirmed-фильтр каталога («Уязвимости и мошеннические схемы») без правок UI. Пользователь 2026-09-09 пересмотрел замороженный запрет spec-widen-web-search («не передавать мошенничество в извлечение/сохранение»): сохранение fraud — новый интент; отдельный раздел ответа и запрет называть fraud лазейкой сохраняются.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_nanobot_graph.py tests/loophole/test_auto_catalog_import.py tests/loophole/test_tools_nanobot.py tests/loophole/test_record_classification_migration.py tests/loophole/test_catalog_classification.py tests/loophole/test_candidate_finding_type_migration.py tests/loophole/test_story_2_2_research_cases.py tests/loophole/test_story_2_5_submission_route.py -q -p no:cacheprovider` — ожидается: PASS.
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole/chat/tools_nanobot.py src/bank_audit/loophole/research_cases.py src/bank_audit/loophole/chat/graph.py` — ожидается: нет новых ошибок.
- `git diff --check` — ожидается: без ошибок пробелов.

## Auto Run Result

Status: done

### Summary of implemented change

Мошеннические схемы, найденные агентом «Лазейки», теперь сохраняются в БД. Контракт извлечения расширен полем `finding_type` ('loophole'|'fraud_scheme'): мошеннические материалы передаются в `audit_extract_loopholes`, извлекаются как положительные находки отдельного типа (`is_loophole=TRUE` по конвенции миграции 066 — «положительная находка любого типа», тип несёт `classification`), персистятся общим путём и автоимпортируются в каталог с `classification='fraud_scheme'` (значение уже поддержано схемой, фильтрами каталога и UI). В тексте ответа мошенничество остаётся отдельным разделом и не называется лазейкой; статистика «по лазейкам» фильтруется `classification='vulnerability'` (промпт 07). Инвариант `update_verdict` не изменён; находки без вердикта по-прежнему отбрасываются. Решение владельца интента: «любая запись без зависимости от флага» — сохранение не зависит от значения is_loophole (fraud+FALSE → not_confirmed, задокументировано), вердикт-гейт сохранён.

### Files changed

- `migrations/067_loophole_candidate_finding_type.sql` — колонка `finding_type` у кандидата (NOT NULL DEFAULT 'loophole' + CHECK через DO-блок, полная идемпотентность; в заголовке — порядок деплоя: миграция до кода).
- `src/bank_audit/loophole/chat/tools_nanobot.py` — парсинг/нормализация `finding_type` в `extract_loopholes` (общий `normalize_finding_type`), проброс в `pending_record`.
- `src/bank_audit/loophole/research_cases.py` — `normalize_finding_type`; `finding_type` в CaseContractV1/add_candidate/persist_managed_run/collect_research; трёхзначный маппинг classification в `import_preliminary_sources` (fraud приоритетен при положительном вердикте, fraud+FALSE → not_confirmed — задокументировано); `finding_type` в case_snapshot и get_candidate.
- `src/bank_audit/loophole/classify.py` — реклассификация сохраняет существующий classification при положительном вердикте.
- `src/bank_audit/loophole/web.py` — KB-пример при ручной маркировке создаётся только для classification='vulnerability' (fraud не загрязняет KB; смена типа удаляет пример).
- `src/bank_audit/loophole/agent/__init__.py` — `candidate_report` помечает тип находки.
- `src/bank_audit/loophole/chat/graph.py` — `finding_type` в `_public_finding` (SSE-проекция карточек).
- `src/bank_audit/loophole/static/loophole.jsx` — бейдж «мошенническая схема» в карточках чата по finding_type.
- `src/bank_audit/loophole/chat/prompt/04_extract_loopholes.md` — контракт finding_type + few-shot пример мошеннической схемы; мошенничество убрано из «не извлекать».
- `src/bank_audit/loophole/chat/prompt/07_nanobot_system.md` — fraud передаётся в извлечение и сохраняется; отдельный раздел ответа и запрет называть схему лазейкой сохранены; статистика по лазейкам — с фильтром classification='vulnerability'.
- Тесты: `test_auto_catalog_import.py`, `test_nanobot_graph.py` (e2e fraud→каталог), `test_tools_nanobot.py` (prompt-контракт, нормализация), `test_classify.py`, `test_catalog_classification.py`, `test_story_2_2_research_cases.py`, `test_story_2_5_submission_route.py`, новый `test_candidate_finding_type_migration.py` (offline текстовый + staging PG).
- `docs/loophole/bmad/implementation-artifacts/spec-loophole-fraud-scheme-persistence.md` — спека (этот файл).

### Review findings breakdown

- Patches applied: 12 (high 0, medium 6, low 6) — см. Review Triage Log.
- Deferred: 2 (дедуп импорта по URL/недетерминированный порядок title-групп; латентное взаимодействие classify_candidates — оба low).
- Rejected: 2 (сохранение находок без вердикта; тесты с живым LLM — вне конвенции проекта).

### Follow-up review recommendation

Patched counts: high 0, medium 6, low 6. Score = 3×6 + 1×6 = 24 (≥ 5) → `followup_review_recommended: true`.

### Verification performed

- `pytest tests/loophole/test_nanobot_graph.py tests/loophole/test_auto_catalog_import.py tests/loophole/test_tools_nanobot.py tests/loophole/test_record_classification_migration.py tests/loophole/test_catalog_classification.py tests/loophole/test_candidate_finding_type_migration.py tests/loophole/test_story_2_2_research_cases.py tests/loophole/test_story_2_5_submission_route.py -q -p no:cacheprovider` — 107 passed, 2 skipped (staging PG, ожидаемо офлайн); прогон родителем после патчей.
- Полный `pytest tests/loophole` — 1077 passed, 4 skipped (прогон субагентом).
- `ruff check` по 6 изменённым source-файлам — только 4 пре-существующие ошибки HEAD, новых нет.
- `git diff --check` — чисто.

### Residual risks

- Миграция 067 должна быть накатана ДО выкатки кода (на проде миграции ручные): иначе INSERT кандидата падает, а persist проглатывает ошибку и молча теряет находки прогона. Порядок зафиксирован в заголовке миграции.
- Staging-прогон миграции 067 на живом PostgreSQL не выполнялся (нужен AUDITLENS_POSTGRES_STAGING_URL); структура покрыта offline текстовым тестом.
- Поведение живой модели (передаёт ли она мошеннические материалы в извлечение) закреплено промптом и few-shot примером; проверяется только на проде — компенсируется тем, что серверный persist-путь для fraud полностью детерминирован и покрыт тестами.
