# План: премиальный стриминг прогресса в UI (deep research v2)

> Цель (дословно): «пользователь ни секунды не должен скучать» — видеть реальный
> прогресс без задержек, премиально как Anthropic Claude (extended thinking).
> Основано на эмпирике + анализе 4 агентов (бэкенд-события, фронт, LLM-вызовы, UX).

## 0. Главный вывод (что физически возможно)

Проверено вживую на cloud.ru Foundation Models (OpenAI-совместимый endpoint):

| Сигнал | Поведение | Вывод |
|---|---|---|
| `delta.reasoning_content` (ход мысли) | **стримится инкрементально**, приходит за 2–4с, РАНЬШЕ ответа | ✅ единственный реальный живой сигнал — на нём строим всё |
| `delta.content` (финальный текст) | **буферизуется проксей**, падает пачкой в конце | ❌ пословный стрим с сервера невозможен → typewriter на фронте |

**Стримим ТОЛЬКО reasoning. Content собираем целиком (как сейчас) → ноль риска для данных/парсинга.**

## 1. Где стримить (LLM-вызовы → reasoning)

Все вызовы сейчас блокирующие (`stream=False`). Кандидаты на reasoning-стрим:

| # | Вызов | Стадия | Длит. | Приоритет |
|---|---|---|---|---|
| 1 | `analyst.write_report` (analyst.py:174) | synthesizing | ~20с | **высокий** — главное тихое окно |
| 2 | `conductor.plan_research` (conductor.py:168) | planning | ~8с | **высокий** — первый экран |
| 3 | `critic.critique_report` (critic.py:111) | critic | ~12с | средний |
| 4 | `_rewrite_with_critique` (orchestrator.py:476) | repair | ~15с | средний |
| 5 | per-agent `_call_llm`/`_extract_final` (base_agent.py:521/658) | research | минуты, ×N агентов | высокий impact / высокий риск |

Содержимое ответов (план-JSON, факты-JSON, отчёт-markdown, вердикт-JSON) собираем **целиком** для существующего парсинга — буферизация прокси этому не мешает.

## 2. Тихие окна, которые закрываем

- **Окно #1 — волна агентов**: между `step_start` и `step_done` каждый агент живёт до 220с в полной тишине (внутри 8–14 итераций поиска/чтения). Сейчас наружу — 2 события на агента.
- **Окно #2 — analyst+critic+repair** (главное, появилось после удаления preview): 20–47с тишины, UI видит только тикающий таймер `_with_heartbeat`.

## 3. Архитектура проброса (консенсус backend + llm агентов)

Проблема: агенты/стадии — это `await`-мир (возвращают результат целиком), а SSE — `yield`-генератор. Моста сейчас нет (`grep asyncio.Queue/callback = 0`).

**Решение — один переиспользуемый хелпер + очередь:**

```python
# research/v2/_streaming.py
async def stream_completion(client, on_reasoning=None, **kw):
    kw["stream"] = True
    # ВАЖНО: вызываем create В ОБХОД throttle-патча (иначе семафор/wall
    # обернут лишь СОЗДАНИЕ стрима, а не его потребление) и держим
    # семафор/wall сами вокруг полного потребления.
    raw_create = getattr(client.chat.completions, "_orig_create",
                         client.chat.completions.create)
    content, reasoning, tcs = [], [], {}
    async def _consume():
        stream = await raw_create(**kw)
        async for ch in stream:
            if not ch.choices: continue
            d = ch.choices[0].delta
            rc = getattr(d, "reasoning_content", None)   # getattr! поля может не быть
            if rc:
                reasoning.append(rc)
                if on_reasoning: await on_reasoning(rc)
            if getattr(d, "content", None): content.append(d.content)
            for tc in (d.tool_calls or []):              # аккумуляция по index
                e = tcs.setdefault(tc.index, {"id":"","name":"","arguments":""})
                if tc.id: e["id"] = tc.id
                if tc.function and tc.function.name: e["name"] += tc.function.name
                if tc.function and tc.function.arguments: e["arguments"] += tc.function.arguments
    async with get_semaphore():
        await asyncio.wait_for(_consume(), timeout=_wall_for(kw))   # 60/75/180с
    return "".join(content), "".join(reasoning), list(tcs.values())
```

- `on_reasoning(chunk)` кладёт `{"type":"reasoning","stage":..,"chunk":..,"n":..,"agent_id":..}` в `asyncio.Queue`, заведённую в `stream_deep_research_v2`.
- Оркестратор на стадиях дренирует очередь параллельно `await`'у стадии: `asyncio.wait([stage_task, queue.get()])` — yield'ит reasoning вперемешку.
- **Идентификация**: per-agent события несут тот же `n = plan.missions.index(m)+1`, что `step_start/step_done` (fan-out даёт несколько агентов с `agent_id='researcher'` — различать по `n`+`subjects[0]`).
- **Коалесинг**: склейка reasoning-чанков по агенту, флаш ~300мс — иначе fan-out (5+ агентов) захлебнёт SSE.

Эталон tool-calling+stream уже есть в `ai/analyst.py:1024-1067` (stream_analysis, quick-путь) — переиспользуем.

## 4. Фронт (app.jsx, in-browser Babel — НЕ greenfield)

Каркас уже готов принять новые события (паттерн `updateLast(patch)`), нет только веток `reasoning`/`agents`. Компоненты ставить **вне** `.dr-doc` grid (на всю ширину, как `ProcessTrace`).

| Компонент | Что делает | Стиль |
|---|---|---|
| **ThinkingPanel** | новая ветка `data.type==='reasoning'` → `updateLast(l=>({reasoning:(l.reasoning||'')+chunk}))`. Приглушённый моно-текст, мигающая каретка, заголовок «Размышляю…» + пульс, бейдж «технический ход мысли · EN». Авто-сворачивание в «Думал N с» когда пошёл `m.text`. | копия `.dr-agent`/`.dr-stage-banner`, `--ink-2`, моно |
| **AgentsPanel** | ветка `data.type==='agents'` (replace-снапшот) → стек 4–6 карточек: точка-статус (ждёт/ищет/читает/думает/готов), бейдж модели, мини-строка живой мысли, прогресс-трек по реальным событиям | по образцу `AgentPanel:1271` |
| **typewriter** | `displayText` + `requestAnimationFrame`, **адаптивная** скорость (80–120 симв/кадр для длинных), кнопка «Показать сразу». Только для пачек >200 симв | курсор `▍` |

**XSS**: reasoning выводить как plain text (`white-space:pre-wrap`), **НЕ через renderMD** (там `dangerouslySetInnerHTML`).
**Перф**: TOC/MutationObserver питать финальным `text`, не покадровым `displayText`.
**Мелочь**: поправить устаревшую подпись модели «Llama 3.3 70B via Fireworks AI» (app.jsx:2225) → gemini.

## 5. UX-принципы (премиально, не цирк)

- Reasoning **не переводим** — это технический ход мысли (decompose/cross-check), перевод добавит задержку и исказит. Подаём как приглушённый серый моно + бейдж EN — честно и стильно (raw chain-of-thought, как Claude).
- Прогресс агента двигается **только по реальному событию инструмента** — между событиями полоса стоит. Никаких самоползущих баров.
- До первого reasoning-токена (~2с) — shimmer-плейсхолдер «Подключаюсь к gemini-3.1-pro…», не спиннер.
- Все анимации (pulse/shimmer/fade, 0.3–1.3с) + **обязательно** `prefers-reduced-motion` (аудиторы сидят часами) + тумблер «спокойный режим».
- **Анти-паттерны (запрещены)**: фейковые прогресс-бары, бесконечные спиннеры, перевод reasoning на лету, скрытие reasoning до конца, typewriter поверх и так инкрементального reasoning.

## 6. Этапы

- **Этап 0 — проверки (перед стартом):** (а) отдаёт ли loop-модель агентов (gemini-2.5-flash, effort=low) reasoning вообще; (б) решить throttle-vs-stream (обходим патч хелпером — рекомендация).
- **Этап 1 — reasoning одиночных стадий (высокий impact, низкий риск):** хелпер `stream_completion` + очередь; включить на analyst → conductor → critic → repair. Фронт: ThinkingPanel. Закрывает главное тихое окно. Данные не трогаем. **За env-флагом `V2_STREAM_REASONING` (дефолт выкл).**
- **Этап 2 — живые параллельные агенты (высокий impact, выше риск):** per-agent reasoning + tool-статус через очередь; активировать мёртвый `agent_tool_call`. Фронт: AgentsPanel. Агентский tool-loop переводить на стрим аккуратно (риск регресса извлечения фактов).
- **Этап 3 — полировка:** typewriter финала, staggered fade-in рейтинга/источников/инсайтов, reduced-motion, подпись модели.

## 7. Риски (главные)

1. **throttle vs stream** (центральное): `call_with_throttle` оборачивает только создание стрима → семафор/wall не покрывают тело, риск 429 и потери wall-защиты. Решение: хелпер вызывает `_orig_create` в обход патча и сам держит semaphore+wall.
2. **tool-calling + stream**: tool_calls приходят фрагментами по index → риск регресса извлечения фактов. На Этапе 1 агентский цикл НЕ трогаем.
3. **reasoning у flash может быть тощим/пустым** (effort=low) — UI не должен ломаться на пустом reasoning. Проверить на Этапе 0.
4. **Объём трафика при fan-out** (5+ агентов × thinking) → коалесинг ~300мс/агент.
5. **EAV-фоллбэк**: ранний reasoning-стрим увеличивает «площадь» — ловить исключения хелпера, не выставлять `yielded` раньше времени.
6. **Комплаенс**: reasoning может содержать сырые формулировки по банкам → фиче-флаг `reasoningVisible` на случай ограничений.
