---
title: 'Сохранение находок агента «Лазейки» в БД и обязательный веб-fallback'
type: 'bugfix'
created: '2026-09-09'
status: 'done'
baseline_commit: '0b30a1c5b2d3f46fd9af76075f05e1abe64d2aec'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: [oversized]
deferred:
  - summary: >-
      Не-SSE путь run_chat сохраняет старый узкий гейт persist (только time_budget)
      и не запускает веб-fallback — политика расходится со stream_chat.
    evidence: |-
      src/bank_audit/loophole/chat/graph.py: run_chat фильтрует ошибки через
      set(result.errors) <= {"time_budget"}; поиск по src показывает, что из web.py
      вызывается только stream_chat, run_chat используется лишь в тестах, хотя его
      docstring заявляет «контракт для web.py».
    location: >-
      src/bank_audit/loophole/chat/graph.py (run_chat)
    severity: low
---

<intent-contract>

## Intent

**Problem:** Агент «Лазейки» не добавляет найденные записи в БД. Корневая причина: в `stream_chat` server-side persist полностью пропускается, если `errors` содержит что-либо кроме `PARTIAL_STOP_MESSAGES`, — а `skill_failed` ставится от ЛЮБОГО упавшего tool-вызова (один флаки `web_fetch` по таймауту/TLS), а `max_iterations` — при типичном длинном исследовании. Подтверждённые находки в `context.pending_records` молча теряются без единой строки в логе. Вторая часть: если по данным (БД/прочитанные источники) мошеннические схемы или уязвимости не найдены, агент не обязан искать в интернете — это лишь мягкая инструкция промпта, которую модель может не выполнить.

**Approach:** (1) Разрешить persist при нефатальных ошибках (`skill_failed`, `max_iterations`) через строгую самопроверяемую валидацию `eligible_findings`, фатальные ошибки (provider/protocol) по-прежнему блокируют persist, но теперь с warning-логом о числе отброшенных находок. (2) Добавить детерминированный серверный веб-fallback: если после запуска нет валидных находок и веб-поиск не выполнялся, сервер сам ищет в интернете по кластерам запросов, читает страницы и извлекает кандидатов существующим механизмом; правило дублируется в системном промпте.

## Boundaries & Constraints

**Always:** Сохранять только находки с явным вердиктом `is_loophole` (True/False), непустыми title/quote, цитатой — подстрокой прочитанного источника и датой в периоде (валидация `eligible_findings` не ослабляется). Мошеннические схемы не сохранять как лазейки и не передавать в `audit_extract_loopholes` (замороженное ограничение spec-widen-web-search). Fallback уважает остаток бюджета времени и лимиты `search_limit`/`fetch_limit`; извлечение идёт через существующий `AuditExtractLoopholesTool` с PII-маскированием. Тесты без сети и реальной БД (SQLite, моки). Русский язык в коде/доках.

**Block If:** Потребуется менять схему БД или миграции, контракт `loophole_record`, сохранять мошеннические схемы в каталог, либо ослаблять `eligible_findings`/период публикации.

**Never:** Не писать в БД из инструментов агента (persist остаётся server-side). Не выполнять persist при фатальных ошибках (`agent_error`, `agent_stream_error`, protocol/provider failure). Не запускать fallback при отмене пользователем. Не извлекать числа/факты из SERP-сниппетов — только из прочитанных страниц. Не создавать git-коммит.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Чистый запуск | Нет errors, есть pending_records | persist + автоимпорт preliminary (как сейчас) | Не ожидается |
| Флаки tool | `skill_failed` (упал один web_fetch), находки валидны | persist по `eligible_findings`, записи в БД | Невалидные отброшены валидатором |
| Лимит итераций | `max_iterations`, находки валидны | persist по `eligible_findings`, записи в БД | partial-пояснение в ответе сохраняется |
| Фатальный сбой | `agent_error`/`agent_stream_error`/protocol error | persist НЕ выполняется | structlog warning: число отброшенных pending + коды ошибок |
| Пустой результат без веб-поиска | 0 валидных находок, `budget.search_results` пуст, бюджет > порога | Fallback: web-поиск по ≥2 независимым кластерам из запроса → fetch топ-страниц → извлечение → находки в pending_records → persist | Сбой поиска/fetch не роняет чат; честный ответ |
| Fallback не нужен | Находки есть ИЛИ веб-поиск уже был ИЛИ бюджет исчерпан ИЛИ отмена | Fallback не запускается | — |

</intent-contract>

## Code Map

- `src/bank_audit/loophole/chat/graph.py` — гейт persist в `stream_chat` (набор
  `_PERSISTABLE_PARTIAL_ERRORS` = `PARTIAL_STOP_MESSAGES ∪ {skill_failed,
  max_iterations, cleanup_timeout}`; фатальные ошибки → structlog-warning
  `loophole_persist_skipped_fatal`), вызов fallback через `_stream_web_fallback`,
  UX-фаза `web_fallback`, замена/дополнение ответа отчётом `candidate_report`.
- `src/bank_audit/loophole/chat/graph.py` — `_persist_confirmed_findings`:
  persist + автоимпорт, уже безопасен (узкий except + rollback); не менять семантику.
- `src/bank_audit/loophole/agent/__init__.py` — `PARTIAL_STOP_MESSAGES`;
  `eligible_findings` (образец валидации); `ManagedAgent._complete_iteration` /
  `_extract_pending_sources` (образец серверного извлечения с ToolContext);
  `ManagedAgent.web_fallback_needed` / `run_web_fallback` / `_web_fallback_research`
  (серверный fallback); `NO_FINDINGS_BUDGET_MESSAGE`, `candidate_report` (в `__all__`).
- `src/bank_audit/loophole/chat/hooks.py` — `AuditHook.record_stream_event` /
  `after_iteration`: источник `skill_failed` от любого failed tool-event.
- `src/bank_audit/loophole/chat/tools_nanobot.py` — `_queue_confirmed_findings`;
  `AuditWebSearchTool`/`AuditWebFetchTool` (кеш/лимиты в budget);
  `AuditExtractLoopholesTool` — переиспользуется в fallback; `ToolContext.fallback_active`
  и `_ensure_tool_active` — scoped-доступ fallback без снятия `budget.cancelled`.
- `src/bank_audit/loophole/run_budget.py` — `ResearchBudget`: `search_results`,
  `search_cache`, `search_limit`, `fetch_limit`, `analysis_status`.
- `src/bank_audit/loophole/chat/prompt/07_nanobot_system.md` — методология
  (правило обязательного веб-поиска при пустых данных); правило ретрая.
- `tests/loophole/test_nanobot_graph.py` — гейт persist (skill_failed / max_iterations /
  cleanup_timeout разрешены, agent_error запрещён + warning-лог), тесты fallback
  (запуск/пропуск/отмена/маппинг активности/замена ответа).
- `tests/loophole/test_auto_catalog_import.py`, `tests/loophole/test_tools_nanobot.py` —
  регрессионные контракты импорта и промпта.

## Tasks & Acceptance

**Execution:**
- `src/bank_audit/loophole/chat/graph.py` — в `stream_chat` расширить допустимый набор ошибок для persist: `PARTIAL_STOP_MESSAGES ∪ {skill_failed, max_iterations}` → находки берутся через `eligible_findings(context)`; при фатальных ошибках persist пропускается с structlog warning (число pending, коды ошибок). Перед persist вызвать fallback агента (ниже), когда нет валидных находок и нет фатальной ошибки.
- `src/bank_audit/loophole/agent/__init__.py` — публичный метод `ManagedAgent` (напр. `run_web_fallback`): условия запуска из I/O-матрицы; детерминированные кластеры запросов из `context.query` (лазейка/схема, мошенничество, обход комиссии); поиск и чтение через существующие tool-классы/`run_blocking_network` с ретраями; извлечение как в `_complete_iteration`; SSE-прогресс через `_activity_events`; строго в остатке бюджета.
- `src/bank_audit/loophole/chat/prompt/07_nanobot_system.md` — в методологию: если по данным базы и прочитанным источникам схемы/уязвимости не найдены — веб-поиск обязателен; «не найдено» без веб-поиска считается ошибкой работы.
- `tests/loophole/test_nanobot_graph.py` — переопределить тест :145 (persist разрешён при skill_failed/max_iterations, запрещён при agent_error); добавить тесты гейта и warning-лога; тесты fallback: запускается при пустом результате без веб-поиска (мок web_search/web_fetch/extract), не запускается при находках/использованном поиске/исчерпанном бюджете/отмене.
- `tests/loophole/test_tools_nanobot.py` — prompt-контракт на правило обязательного веб-fallback.

**Acceptance Criteria:**
- Given валидные подтверждённые находки, when стрим завершился с `skill_failed` от одного упавшего `web_fetch` или с `max_iterations`, then находки сохранены в `loophole_research` и автоимпортированы в каталог со статусом `preliminary`.
- Given pending-находки, when запуск завершился provider/protocol-ошибкой, then persist не выполняется и в логе warning с числом отброшенных находок.
- Given 0 валидных находок и неиспользованный веб-поиск, when запуск завершается с остатком бюджета, then сервер выполняет веб-поиск минимум по 2 независимым кластерам, читает найденные страницы и прогоняет извлечение; валидные кандидаты сохраняются.
- Given веб-поиск уже выполнялся агентом или бюджет исчерпан или запрос отменён, when запуск завершается без находок, then fallback не запускается и ответ честно фиксирует отсутствие находок.

## Spec Change Log

## Review Triage Log

### 2026-09-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 14: (high 1, medium 4, low 9)
- defer: 1: (low 1)
- reject: 3
- addressed_findings:
  - `[high]` `[patch]` Ответ пользователя оставался «не найдено» после успешного fallback (замена только по литералу-маркеру, нет доставки при streamed_any, устаревший materials-блок, нет теста) — исправлено: замена всего блока маркер+materials или дополнение отчётом, доставка отдельным token-событием, hook.final_answer синхронизирован, 2 новых теста.
  - `[medium]` `[patch]` `cleanup_timeout` молча блокировал persist — добавлен в persistable-набор, тест.
  - `[medium]` `[patch]` `budget.stop_reason` перезаписывался fallback без синхронизации с hook/SSE — восстановление в finally, тест.
  - `[medium]` `[patch]` Снятие `budget.cancelled` ослабляло защиту от поздних subagent-записей — scoped-флаг `ToolContext.fallback_active`, тест.
  - `[medium]` `[patch]` Нет per-call timeout на search/fetch fallback — обёрнуты в `asyncio.timeout(min(45, остаток))`.
  - `[low]` `[patch]` Кластеры строились из PII-маскированного длинного запроса — очистка плейсхолдеров, лимит 80 символов, тест.
  - `[low]` `[patch]` TimeoutError поиска прерывал цикл (0–1 кластеров), failed-поиски засчитывались в квоту «≥2» — continue и учёт только успешных.
  - `[low]` `[patch]` Вводящие события при search_limit/fetch_limit — пред-проверки лимитов до эмита running.
  - `[low]` `[patch]` Извлечение «долбило» при исчерпанном бюджете; константа MAX_FETCHES использовалась для двух целей — ранний выход при остатке ≤10 с, отдельная `_WEB_FALLBACK_MAX_EXTRACTIONS`, комментарии к магическим числам.
  - `[low]` `[patch]` Приватный `_candidate_report` импортировался из graph.py — публичный `candidate_report`, оба имени в `__all__`.
  - `[low]` `[patch]` Нет UX-индикатора fallback — фаза `web_fallback` перед запуском.
  - `[low]` `[patch]` Пробелы тестов: отмена в середине fallback, маппинг `audit_web_fetch` в SSE, fallback при partial-ошибке `no_progress` — 3 новых теста.
  - `[low]` `[patch]` Устаревший Code Map спеки и незафиксированные дизайн-решения — обновлены (вне intent-contract).
  - deferred: расхождение политики persist/fallback в не-SSE пути `run_chat` (мёртвый прод-путь, вызывается только из тестов).
  - rejected: требование e2e-проверки против реальной БД/сети (противоречит конвенции тестов проекта — без сети и БД); чтение B3 «искать в интернете повторно, даже если поиск уже был» (обязательство выполняется одним веб-поиском за запуск); «причина декларирована, но не продемонстрирована в рантайме» (причина определена анализом кода и зафиксирована в спеке — runtime-следы прода локально недоступны).

## Design Notes

Persist при `skill_failed`/`max_iterations` безопасен, потому что `eligible_findings` самопроверяем: явный вердикт, цитата — подстрока текста прочитанного источника, дата в периоде. Доказательная сила находки не зависит от того, чем завершился запуск; флаки одного fetch не обесценивает уже собранные доказательства. Фатальные ошибки (провайдер/протокол) означают недоверенное состояние ответа — persist остаётся запрещён, но перестаёт быть молчаливым.

Fallback вынесен в код, а не только в промпт, по принципу проекта «детерминированный код вместо надежды на LLM»: модель может проигнорировать инструкцию, серверный этап — нет. Fallback переиспользует `_complete_iteration`-механику (общий хелпер `_extract_pending_sources`), поэтому его находки проходят ту же валидацию и тот же persist.

Зафиксированные дизайн-решения по итогам ревью:

- **SSE-прогресс fallback — polling очереди.** `_stream_web_fallback` запускает
  `run_web_fallback` задачей и опрашивает `_activity_events` с интервалом 0.5 с,
  стримя `audit.tool`-события (`audit_web_search`/`audit_web_fetch`/
  `audit_extract_loopholes`) через `_map_event`; перед запуском эмитится UX-фаза
  `web_fallback`. Потребление вложенного генератора обёрнуто в try/finally с
  явным `aclose`: `async for` сам не закрывает вложенный генератор, а его
  finally отменяет задачу fallback при отключении клиента.
- **Ответ после успешного fallback.** Если `candidate_report` по валидным
  находкам непуст: при наличии `NO_FINDINGS_BUDGET_MESSAGE` заменяется весь
  устаревший блок (маркер + materials-отчёт с «извлечение не завершено»), иначе
  отчёт дописывается через пустую строку; `hook.final_answer` синхронизируется.
  При `streamed_any` отчёт доставляется отдельным token-событием в финальной
  секции — UI не остаётся с текстом «не найдено» при сохранённых записях.
- **Scoped-доступ вместо снятия `budget.cancelled`.** Fallback работает со
  своим `ToolContext(fallback_active=True)`; `_ensure_tool_active` для такого
  контекста игнорирует глобальный `cancelled` (выставлен при штатном завершении
  запуска), но сохраняет дедлайн и остановку по `requested_count`. Контексты
  фоновых subagent флага не имеют, поэтому их поздние записи по-прежнему
  отсекаются. `budget.stop_reason` после fallback всегда восстанавливается —
  SSE/AgentResult/аудит не расходятся с терминальным состоянием запуска.

Остаточные известные риски (вне scope, не баги этой задачи): rollback при обрыве SSE между persist и коммитом (`web.py:819`); дедупликация автоимпорта при повторных исследованиях по тем же URL — осознанное поведение.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_nanobot_graph.py tests/loophole/test_auto_catalog_import.py tests/loophole/test_tools_nanobot.py -q -p no:cacheprovider` — ожидается: PASS.
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole/chat/graph.py src/bank_audit/loophole/agent/__init__.py tests/loophole/test_nanobot_graph.py tests/loophole/test_tools_nanobot.py` — ожидается: нет новых ошибок.
- `git diff --check` — ожидается: без ошибок пробелов.

## Auto Run Result

Status: done

### Summary of implemented change

Исправлена потеря находок агента «Лазейки» и добавлен обязательный веб-fallback.

**Корневая причина бага (часть 1):** в `stream_chat` server-side persist пропускался при любом коде в `errors` за пределами `PARTIAL_STOP_MESSAGES`. `skill_failed` выставлялся от любого упавшего tool-вызова (например, один флаки `web_fetch` по таймауту/TLS), а `max_iterations` — при типичном длинном исследовании; подтверждённые находки в `context.pending_records` молча отбрасывались. Исправление: набор persistable-ошибок расширен до `PARTIAL_STOP_MESSAGES ∪ {skill_failed, max_iterations, cleanup_timeout}`, находки при таких ошибках проходят строгую валидацию `eligible_findings` (явный вердикт, цитата ⊂ текст прочитанного источника, дата в периоде). Фатальные ошибки (`agent_error`, `agent_stream_error`, сбой протокола) по-прежнему блокируют persist, но теперь логируются structlog-warning `loophole_persist_skipped_fatal` с числом отброшенных находок.

**Часть 2 — обязательный веб-fallback:** если запуск завершился без валидных находок и веб-поиск не выполнялся (пустые `budget.search_results`/`search_cache`), сервер детерминированно ищет в интернете по ≥2 независимым кластерам запросов, читает найденные страницы и прогоняет извлечение существующим механизмом (PII-маскирование, лимиты, остаток бюджета ≥60 с). Найденные кандидаты сохраняются общим persist-путём; ответ пользователя обновляется отчётом (включая доставку при уже отстримленном ответе). Правило продублировано в системном промпте агента. Мошеннические схемы по-прежнему не сохраняются как лазейки (замороженное ограничение).

### Files changed

- `src/bank_audit/loophole/chat/graph.py` — новый гейт persist с warning-логом; вызов fallback с SSE-прогрессом (`_stream_web_fallback`, фаза `web_fallback`); обновление ответа отчётом `candidate_report`.
- `src/bank_audit/loophole/agent/__init__.py` — `ManagedAgent.run_web_fallback`/`web_fallback_needed`: кластеры с очисткой PII-плейсхолдеров, per-call timeout, квота только успешных поисков, scoped-флаг `fallback_active`, восстановление `stop_reason`; общий хелпер `_extract_pending_sources` с ранним выходом; публичные `candidate_report`/`NO_FINDINGS_BUDGET_MESSAGE` в `__all__`.
- `src/bank_audit/loophole/chat/tools_nanobot.py` — `ToolContext.fallback_active` и его учёт в `_ensure_tool_active`.
- `src/bank_audit/loophole/chat/prompt/07_nanobot_system.md` — правило: при пустых данных веб-поиск обязателен, «не найдено» без веб-поиска — ошибка работы.
- `tests/loophole/test_nanobot_graph.py` — переопределён тест запрета persist (fatal-only + capture_logs), +11 тестов: persist при skill_failed/max_iterations/cleanup_timeout, fallback (запуск/пропуск/отмена/partial-ошибки/маппинг fetch/доставка отчёта/замена маркера).
- `tests/loophole/test_tools_nanobot.py` — prompt-контракт обязательного веб-fallback.
- `docs/loophole/bmad/implementation-artifacts/spec-loophole-agent-records-persist-and-web-fallback.md` — спека (этот файл).

### Review findings breakdown

- Patches applied: 14 (high 1, medium 4, low 9) — см. Review Triage Log.
- Deferred: 1 (расхождение политики в мёртвом не-SSE пути `run_chat`, low).
- Rejected: 3 (e2e против реальной БД/сети вне конвенции тестов; чтение «искать повторно, даже если поиск уже был»; требование runtime-демонстрации причины).

### Follow-up review recommendation

Patched counts: high 1, medium 4, low 9. Score = 3×4 + 1×9 = 21 (≥ 5), а также есть high-находка → `followup_review_recommended: true`.

### Verification performed

- `pytest tests/loophole/test_nanobot_graph.py tests/loophole/test_auto_catalog_import.py tests/loophole/test_tools_nanobot.py -q -p no:cacheprovider` — 54 passed (прогон родителем после патчей).
- Полный `pytest tests/loophole` — 1066 passed, 3 skipped (прогон субагентом).
- `ruff check` по 5 изменённым файлам — 4 ошибки, все подтверждены пре-существующими на HEAD (пофайловая сверка: HEAD 4 = после изменений 4), новых нет.
- `git diff --check` — чисто.
- Frontmatter спеки распарсен как YAML: `deferred` — список из 1 элемента с заданным текстом.

### Residual risks

- Rollback при обрыве SSE между persist и коммитом (`web.py:819`) — клиент, закрывший вкладку в этом окне, теряет записи (пре-существующее, вне scope).
- Дедупликация автоимпорта: повторное исследование по тем же URL осознанно не добавляет записей — снаружи может выглядеть как «не сохраняет».
- Fallback не стартует при очень маленьком `agent_timeout_seconds` (законно, порог 60 с).
- Проверка на проде потребует рестарта сервиса; end-to-end сценарий с живой моделью и сетью локально не воспроизводился (конвенция тестов — без сети/БД).
