# 01 — Переименовать модуль «Уязвимости» в «Лазейки»

Status: ready-for-agent

## Зачем

Канонический язык зафиксирован в глоссарии (`docs/loophole/matt/GLOSSARY.md:11-13`):
модуль называется «Лазейки», а «Уязвимости» помечено как избегаемое — «это только
заголовок в UI». Сейчас UI и производные тексты всё ещё называют модуль
«Уязвимости»: название расходится с кодом (модуль `loophole`), глоссарием и
доменной моделью (`DOMAIN-MODEL.md:273`), а заодно звучит как ИБ/CVE-термин и
вводит аудиторов в заблуждение — модуль про правовые и процессные слабости, а не
про уязвимости безопасности.

Переименовываем только имя модуля. Тип находки «уязвимость» (классификация
наряду с «мошеннической схемой») — отдельный термин предметной области, его не
трогаем.

## Целевые строки

- Короткое имя (меню, брейдкрамб, теги, подписи): «Уязвимости» → **«Лазейки»**.
- Полное имя (заголовок вкладки, `<title>`, title iframe): «Уязвимости и
  мошеннические схемы» → **«Лазейки и мошеннические схемы»**.
- Двойные ряды «лазейки и уязвимости» (заголовки отчётов, docstrings) →
  **«Лазейки и мошеннические схемы»** / «лазейки и мошеннические схемы» по падежу.
- Склонения — по контексту: «раздел "Лазейки"», «вкладка "Лазейки"», «записи
  раздела "Лазейки"». Неестественные падежные конструкции переформулировать
  (например «Запись "Уязвимостей"» → «Запись раздела "Лазейки"»).

Общее правило для всех файлов: менять только вхождения, называющие модуль/раздел;
вхождения слова «уязвимость» в значении типа записи или общем смысле остаются
(см. «Что не трогаем»).

## Что меняем

### A. UI-статика

- `src/bank_audit/web/static/index.html:209` — CSS-комментарий.
- `src/bank_audit/web/static/app.jsx` — `736` (тег «Уязвимости · Сбер»), `738`,
  `8600` (title iframe), `8823` и `9901` (брейдкрамбы), `9890` (пункт меню),
  `9899` (комментарий).
- `src/bank_audit/loophole/static/loophole.html:7` — `<title>`.
- `src/bank_audit/loophole/static/loophole.jsx` — `1`, `373`, `2021` (комментарии),
  `2263` (fallback-заголовок), `2490` (нет доступа), `2519` (пустое состояние).
- `src/bank_audit/loophole/static/loophole.css:1` — комментарий (строка 5 — общий
  смысл, не трогать).
- Пересобрать предсборку: `node scripts/build_loophole_js.mjs`; в коммит идут оба
  файла `loophole.jsx` и `loophole.js` (тест сверяет sha256). Заодно
  `scripts/build_loophole_js.mjs:1` — комментарий.

### B. Бэкенд — подписи и генерируемые тексты

- `src/bank_audit/digest/writer.py:456,675`.
- `src/bank_audit/research/gptr/brief.py:36,123`; `dossier.py:59,234,428,532`
  (59 и 428 — двойные ряды → «Лазейки и мошеннические схемы»);
  `own_data.py:11,36,87,457,462,475`.
- `src/bank_audit/ai/hermes_quick.py:97` (метка инструмента → «лазейки»);
  `agent_tools.py:630,684,786-787` (786-787 — label в `ToolSpec`);
  `agent_eval.py:280,299,301,341,343,538,539` (имена кейсов и вопросы; 539 —
  двойной ряд).
- `src/bank_audit/loophole/__init__.py:1` (двойной ряд); `pdf_export.py:37`
  (двойной ряд); `catalog_export.py:1,34,37,62,117,126,148,150` (docstring, темы
  писем, eyebrow, заголовки Excel); `summary.py:1`; `repository.py:639`.
- В `summary.py`, `repository.py`, `web.py` остальные совпадения относятся к типу
  записи «уязвимость» — их не трогаем.

### C. Живая документация и конфиги агентов

- `AGENTS.md:31,166` — упоминания модуля.
- `DESIGN.md:1,73,116`.
- `docs/project-context.md:231` (селектор iframe под новую строку title), `353`.
- `docs/loophole/matt/GLOSSARY.md:13` — переформулировать: «Уязвимости» больше не
  заголовок UI, а устаревшее имя модуля.
- `docs/loophole/matt/domain-model/DOMAIN-MODEL.md:273` — строка таблицы:
  «код и UI: "Лазейки"».
- `deploy/hermes-al/SOUL.md:11`;
  `deploy/hermes-al/skills/auditlens-data/SKILL.md:3,18,32,49,55,71`;
  `deploy/hermes-al/skills/auditlens-loopholes/SKILL.md:3,9,13,16,24,25,44`.

### D. Тесты (только ассерты строк; имена файлов не меняем)

- `tests/loophole/test_vulnerabilities_redesign_ui.py:1,46,48,53,54,56,214,231,
  310,362,417,580,607,626` — по общему правилу (строки про модуль → «Лазейки»;
  строки про тип записи, например `607` «Уязвимости и схемы», — не трогать).
- `tests/loophole/test_vulnerabilities_redesign_contract.py:1,6,133`;
  `tests/loophole/test_vulnerabilities_redesign_api.py:1,40,41,59,63,65,197,198,
  241,250,259,311`.
- Прочие в `tests/loophole/`: `test_catalog_classification_runtime.py:35,94,107,
  121,126,127`; `test_catalog_classification.py:14,108,110`;
  `test_final_layout_runtime.py:194-196,909`;
  `test_verdict_authorization_runtime.py:37,101,142`;
  `test_queue_verdict_comments_authors.py:157,220,316,365,390`;
  `test_auto_catalog_import.py:326,352,486,497,553,628`;
  `test_story_2_2_research_cases.py:556,570,578`;
  `test_preliminary_research_source_import.py:122`; `test_refresh_button.py:1`.
- Корневые: `tests/test_hermes_agent.py:79,263,264` (метка инструмента —
  синхронно с `hermes_quick.py:97`); `tests/test_deep_own_data.py:167` (двойной
  ряд — синхронно с `dossier.py:59`).

## Что не трогаем

- **Тип записи «уязвимость»** и его UI-метки: enum `vulnerability` в коде и
  данных; метки «Уязвимость» / «Уязвимости» / «Уязвимости и схемы» как тип
  (`loophole.jsx:409,414,419,1960,2461,2603-2606,2669,2670,3094,3706`;
  `catalog_export.py:31,161`; `models.py:30`; `web.py:244`; SQL в
  `repository.py`; `tests/loophole/conftest.py:61,325`; CHECK в миграциях
  049/066/067).
- **Отзывы «уязвимые клиенты»**: признак `vulnerable`, фильтры `vuln:*`
  (`app.jsx`, `web/userdata.py`, `web/case_export.py`, `web/reviews_export.py`,
  `rag/reviews_*`, `rag/review_*`, `config/review_codebook/`, миграция 069,
  `docs/ARCHITECTURE.md:311,325,329`, `test_exports.py`,
  `test_review_analytics.py`, `test_review_annotate.py`).
- **Промпты модели** с общим смыслом «уязвимость»: `loophole/classify.py:25`,
  `loophole/chat/prompt/02_plan.md:3`, `03_react.md:7`,
  `09_loopholes_prompt.md:1-2`.
- **Регэкспы стоп-слов**: `ai/agent_tools.py:625`, `ai/agent_eval.py:516`.
- **Примеры-подсказки** с общим смыслом (`loophole.jsx`, SUGGESTIONS, напр.
  `3204`).
- **Миграции целиком** (решение «БД не трогаем»; комментарии в них остаются как
  есть; enum и CHECK тем более).
- **Имена тест-файлов** `test_vulnerabilities_redesign_*.py` (идентификаторы;
  ссылки на них в `DESIGN.md:256,265` остаются валидными).
- **Исторические артефакты**: `docs/loophole/bmad/**`, `docs/loophole/reviews/`.
- **Скриншоты** `docs/img/` — обновляются вне этого тикета (см. «Хвосты»).

## Критерии приёмки

1. `pytest` — весь набор зелёный, включая loophole-набор и тест sha256 на
   `loophole.js`.
2. `ruff check src` — без замечаний.
3. `loophole.js` пересобран (`node scripts/build_loophole_js.mjs`) и закоммичен
   вместе с `loophole.jsx`.
4. `grep -ri "уязвим"` и `grep -ri "vulnerab"` по `src/`, `deploy/hermes-al/`,
   `AGENTS.md`, `DESIGN.md`, `docs/project-context.md` находят только строки из
   списка «Что не трогаем».
5. Меню, брейдкрамб, заголовок вкладки и `<title>` показывают «Лазейки» /
   «Лазейки и мошеннические схемы»; тип записи «Уязвимость» в фильтрах,
   вердиктах и БД не изменился.

## Хвосты (вне тикета)

- Скриншоты `docs/img/` (в т.ч. `07_loophole.png`) содержат старое название —
  переизготовить при следующем прогоне документации.
- `docs/loophole/bmad/**` остаётся со старым названием как исторический архив.

## Comments
