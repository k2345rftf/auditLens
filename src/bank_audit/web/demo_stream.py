"""Demo-режим стриминга для записи видео.

Активируется когда:
  1. В .env задан DEMO_MODE=1
  2. Вопрос содержит триггерные ключи (см. demo/responses/*.json `trigger_keywords`)

Стримит SSE-события с realistic-таймингом для эффектной презентации:
  - planner (1.5s) → видны 12 шагов
  - discovery (~6s) → источники добавляются 2 батчами (не 12 раз)
  - fact-extract (3s) → счётчик «верифицировано/отфильтровано»
  - synth streaming (12-14s) → text появляется абзацами
  - charts (1.5s) → 3 графика встроены inline через [[CHART:N]]
  - done

Общее время: ~28-30 секунд. UI не дёргается (batched sources updates,
chunks по абзацам — не по 30 chars).
"""
from __future__ import annotations
import asyncio, json, logging, os, re, random
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)

DEMO_DIR = Path(__file__).resolve().parent.parent.parent.parent / "demo" / "responses"


def _load_demo_responses() -> list[dict]:
    if not DEMO_DIR.exists():
        return []
    out = []
    for f in sorted(DEMO_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("demo: cannot load %s: %s", f.name, e)
    return out


_RESPONSES_CACHE: list[dict] | None = None


def _get_responses(reload: bool = False) -> list[dict]:
    global _RESPONSES_CACHE
    if _RESPONSES_CACHE is None or reload:
        _RESPONSES_CACHE = _load_demo_responses()
    return _RESPONSES_CACHE


def find_demo_response(question: str) -> dict | None:
    """Triggers по подстроке. Reload каждый раз — для удобства итераций
    над JSON во время подготовки записи (без перезапуска uvicorn)."""
    q_low = (question or "").lower()
    for resp in _get_responses(reload=True):
        for t in (resp.get("trigger_keywords") or []):
            if t.lower() in q_low:
                return resp
    return None


def is_demo_mode_active() -> bool:
    return os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes", "on")


def _split_into_paragraphs(text: str) -> list[str]:
    """Разбивает markdown на стримящиеся абзацы.

    Сохраняет блочные элементы целиком (таблица, список, [[CHART:N]] — одним
    блоком), параграфы по 1 шт. Возвращает list строк с trailing '\\n\\n'.
    """
    blocks: list[str] = []
    cur: list[str] = []
    in_table = False
    in_list = False

    def flush():
        if cur:
            blocks.append("\n".join(cur).rstrip() + "\n\n")
            cur.clear()

    for line in text.split("\n"):
        s = line.rstrip()
        # Чарт-маркер всегда сам по себе
        if re.match(r"^\s*\[\[CHART:\d+\]\]\s*$", s):
            flush()
            blocks.append(s + "\n\n")
            in_table = False; in_list = False
            continue
        # Таблица — собираем подряд идущие строки с | в один блок
        if s.startswith("|"):
            if not in_table:
                flush()
                in_table = True
            cur.append(s)
            in_list = False
            continue
        elif in_table:
            flush()
            in_table = False
        # Заголовок — отдельный блок
        if re.match(r"^#{1,6}\s+", s):
            flush()
            cur.append(s); flush()
            in_list = False
            continue
        # Списки — копим в один блок
        if re.match(r"^[\*\-•]\s+", s) or re.match(r"^\d+\.\s+", s):
            if not in_list:
                flush()
                in_list = True
            cur.append(s)
            continue
        elif in_list and s == "":
            flush()
            in_list = False
            continue
        # Пустая строка между абзацами
        if s == "":
            flush()
            continue
        cur.append(s)
    flush()
    return [b for b in blocks if b.strip()]


async def stream_demo_response(question: str, resp: dict) -> AsyncIterator[str]:
    """Эмулирует SSE-события полного deep-research'а из готового resp.

    Цель: 28-30 секунд, плавная анимация без autoscroll-jerk'ов.
    """
    log.warning("[DEMO] streaming pre-baked response (triggers=%s)",
                 resp.get("trigger_keywords"))

    plan = resp.get("plan", [])
    sources = resp.get("sources", [])
    outline = resp.get("outline", [])
    charts = resp.get("charts", [])
    verification = resp.get("verification", {})
    coverage = resp.get("coverage", {})
    report_md = resp.get("report_md", "")

    # ── 0. Mode signal — БЕЗ этого UI рендерит quick-bubble (нет .dr-doc-main,
    # .dr-doc-toolbar, .dr-rail). Critical!
    yield json.dumps({"type": "mode", "value": "deep"}, ensure_ascii=False)

    # ── 1. Planning (1.5s) ─────────────────────────────────────────────
    yield json.dumps({"type": "phase", "value": "planning"}, ensure_ascii=False)
    yield json.dumps({"type": "stage_status", "stage": "planning",
                       "label": "Resolver + Planner",
                       "detail": "LLM понимает тему и формирует план",
                       "estimate_s": 2}, ensure_ascii=False)
    await asyncio.sleep(1.5)
    yield json.dumps({"type": "plan", "steps": plan}, ensure_ascii=False)
    await asyncio.sleep(0.3)

    # ── 2. Discovery (~6s) ─────────────────────────────────────────────
    # КЛЮЧЕВОЕ ОТЛИЧИЕ от прошлой версии: источники приходят 2 батчами
    # (а не 12 раз по одному). UI делает только 2 re-render'а sources panel
    # вместо 12 — нет дёрганья при autoscroll.
    yield json.dumps({"type": "phase", "value": "discovery"}, ensure_ascii=False)
    yield json.dumps({"type": "stage_status", "stage": "discovery",
                       "label": "Сбор источников",
                       "detail": "semantic_search + fetch_official + web + reviews",
                       "estimate_s": 6}, ensure_ascii=False)

    # Видимый прогресс плана: step_start → wait → step_done — БЕЗ повторных
    # sources events. step_done несёт `found` count для UI-индикатора.
    first_batch_step = max(2, len(plan) // 2)   # первая половина шагов
    for i, step in enumerate(plan):
        n = step.get("n")
        yield json.dumps({"type": "step_start", "n": n,
                           "title": step.get("title"),
                           "tool": step.get("tool"),
                           "entity": step.get("entity")}, ensure_ascii=False)
        await asyncio.sleep(0.18)
        # step_done: количество найденных «источников» (для индикатора)
        found_n = 1 + (n % 2)   # 1 или 2 — псевдо-разнообразие
        yield json.dumps({"type": "step_done", "n": n,
                           "found": found_n, "used": found_n}, ensure_ascii=False)
        await asyncio.sleep(0.08)
        # После первой половины — кидаем половину sources одним батчем
        if i + 1 == first_batch_step:
            half = max(1, len(sources) // 2)
            yield json.dumps({"type": "sources", "sources": sources[:half]},
                              ensure_ascii=False)
            await asyncio.sleep(0.2)
    # Финальный батч всех sources
    yield json.dumps({"type": "sources", "sources": sources}, ensure_ascii=False)
    await asyncio.sleep(0.3)

    # ── 3. Coverage report ─────────────────────────────────────────────
    yield json.dumps({"type": "coverage",
                       "total_sources": coverage.get("total_sources", len(sources)),
                       "high_trust": coverage.get("high_trust", 0),
                       "mid_trust":  coverage.get("mid_trust", 0),
                       "low_trust":  coverage.get("low_trust", 0),
                       "warning": None}, ensure_ascii=False)
    await asyncio.sleep(0.3)

    # ── 4. Fact-extract phase ──────────────────────────────────────────
    yield json.dumps({"type": "phase", "value": "fact-extract"}, ensure_ascii=False)
    yield json.dumps({"type": "stage_status", "stage": "fact-extract",
                       "label": "Извлечение фактов (per-bank parallel)",
                       "detail": "4 банка параллельно: тарифы, сроки, операции, ограничения",
                       "estimate_s": 3}, ensure_ascii=False)
    await asyncio.sleep(3.2)

    # Verification — anti-hallucination signal.
    # ВАЖНО: `unverified` должен быть МАССИВом объектов {claim, issue} —
    # JSX делает `.map(...)` для PdfExportButton. Если в JSON это число —
    # автоматически конвертируем в массив-пустышку нужной длины.
    _unv = verification.get("unverified", 0)
    if isinstance(_unv, int):
        _unv = [{"claim": f"Факт #{i+1}", "issue": "не подтверждён в источниках"}
                  for i in range(_unv)]
    yield json.dumps({"type": "verification",
                       "verified":   verification.get("verified", 0),
                       "unverified": _unv,
                       "drop_rate":  verification.get("drop_rate", 0)},
                      ensure_ascii=False)
    await asyncio.sleep(0.3)

    # ── 5. Outline (план отчёта появляется заранее) ────────────────────
    yield json.dumps({"type": "outline", "sections": outline}, ensure_ascii=False)
    await asyncio.sleep(0.4)

    # ── 6. Synth streaming ─────────────────────────────────────────────
    # Стримим АБЗАЦАМИ (а не chars по 30) — UI red-render'ит реже, выглядит
    # естественнее, читабельнее. Цель: ~13 секунд на 12-15 KB markdown.
    yield json.dumps({"type": "phase", "value": "synthesizing"}, ensure_ascii=False)
    yield json.dumps({"type": "stage_status", "stage": "synth",
                       "label": "Синтез отчёта",
                       "detail": "LLM пишет первый драфт по собранным данным",
                       "estimate_s": 13}, ensure_ascii=False)
    await asyncio.sleep(0.5)

    paragraphs = _split_into_paragraphs(report_md)
    if not paragraphs:
        paragraphs = [report_md]

    # Целевое время на streaming
    target_seconds = 13.0
    delay_per_block = max(0.05, target_seconds / max(len(paragraphs), 1))

    for i, block in enumerate(paragraphs):
        # Чарт-блок отдаём чуть медленнее с акцентом
        is_chart = bool(re.match(r"^\s*\[\[CHART:\d+\]\]", block))
        yield json.dumps({"type": "text", "chunk": block}, ensure_ascii=False)
        # jitter ±20% для естественности
        await asyncio.sleep(delay_per_block * random.uniform(0.85, 1.2))
        # После графика чуть подольше пауза — пользователь успевает увидеть
        if is_chart:
            await asyncio.sleep(0.4)

    await asyncio.sleep(0.4)

    # ── 7. Charts (отдельные events для UI-компонента ChartCanvas) ─────
    # ВАЖНО: charts шлём ВСЕГДА (а не только если их нет в markdown'е) —
    # renderMD на стороне фронта парсит [[CHART:N]] и берёт specs из этого
    # списка. Без charts events маркеры будут пустыми.
    yield json.dumps({"type": "phase", "value": "charting"}, ensure_ascii=False)
    yield json.dumps({"type": "stage_status", "stage": "charts",
                       "label": "Генерация графиков",
                       "detail": "Извлечение числовых сравнений из фактуры",
                       "estimate_s": 2}, ensure_ascii=False)
    await asyncio.sleep(0.8)
    for ch in charts:
        yield json.dumps({"type": "chart", "spec": ch}, ensure_ascii=False)
        await asyncio.sleep(0.15)

    await asyncio.sleep(0.4)

    # ── 8. Done ────────────────────────────────────────────────────────
    yield json.dumps({"type": "phase", "value": "done"}, ensure_ascii=False)
    yield json.dumps({"type": "done"}, ensure_ascii=False)
    log.warning("[DEMO] response streamed successfully (~%s blocks)", len(paragraphs))
