"""Unified Synthesis — ОДИН аналитический проход вместо 9 narrative-генераторов.

Зачем (этап 6 рефакторинга): раньше отчёт собирали 9 отдельных LLM-генераторов
(key_findings, per_entity_breakdown, pricing_breakdown, regulatory_box, cant_do_box,
requirements_box, digital_channels, government_programs, conflict_explainer) +
outline_planner. Они дублировали данные (comparison_table / pricing / per_entity
показывали одни и те же fee/rate/limit ТРИЖДЫ — «вода»), каждый со своим промптом,
keyword-триггером, fallback и verify. Это и есть переусложнение, на которое жаловался
владелец.

Теперь: ОДИН сильный reasoning-вызов получает всю картину (факты со слотами и
условиями, матрица-дельты, меморандум-brief, выдержки) и сам пишет связный отчёт,
решая структуру по наличию данных. LLM делает РАССУЖДЕНИЕ; код вокруг — только
детерминированная сантехника (сравнительная таблица, методология, источники, пробелы
рендерятся отдельно в narrative_renderer; здесь — проверка чисел и цитат).

За флагом SYNTH_UNIFIED (по умолчанию вкл). Старые 9 генераторов остаются как
fallback (SYNTH_UNIFIED=0) на время A/B.
"""
from __future__ import annotations
import asyncio, logging, os

from .base import (
    NarrativeContext,
    verify_numbers_in_text,
    enforce_citations,
    build_npa_haystack,
    annotate_unverified_npa,
    format_facts_for_prompt,
    select_facts_for_section,
    get_default_model,
)
from ..fact import Fact

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — главный аудитор-аналитик банковских продуктов (внутренний
аудит, не маркетинг). Пишешь ОДИН связный аналитический отчёт по сравнению продукта
между банками для коллеги-аудитора. Цена ошибки высокая → достоверность важнее красоты.

ВХОД: вопрос аудитора, факты по банкам (со слотами, условиями, цитатами [N]),
сводка различий (дельты), аналитический меморандум, дословные выдержки источников.

СТРУКТУРА ОТЧЁТА — выбирай секции ПО НАЛИЧИЮ ДАННЫХ (не плоди пустые):
  1. **Ключевые выводы** (3-5) — НЕ пересказ фактов, а аналитика: где банки расходятся
     сильнее всего (с конкретными числами и «в N раз / на X ₽»), витрина↔реальность,
     скрытые условия. ОБЯЗАТЕЛЬНО раскрой крупнейшие дельты из сводки.
  2. **Рейтинг банков по продукту** — если вопрос про сравнение/рейтинг: упорядочь банки
     с КРАТКИМ обоснованием на основе извлечённых параметров (дешевле/выгоднее/гибче).
     Если данных по банку мало — честно скажи «недостаточно данных для оценки».
  3. **Разбор по банкам** — по 2-4 предложения на банк С ДАННЫМИ (стратегия, условия,
     подвохи). Банки без данных — одной строкой «нет данных в открытых источниках».
  4. **Стоимость и тарифы** — только если есть fee/rate/limit: разбери ключевые цены,
     НЕ повторяя сравнительную таблицу дословно (она уже отрисована отдельно).
  5. **Требования / ограничения / регуляторика** — только если есть такие факты.
  6. **Жалобы клиентов** — если в источниках есть отзывы/претензии по продукту.
  7. **Риски и рекомендации аудитору** — КОНКРЕТНЫЕ, привязанные к числам/банкам
     («X дороже Y на N ₽ — проверить условие Z из [n]»), а не общие «запросить тарифы».

ЖЁСТКИЕ ПРАВИЛА:
  • КАЖДОЕ число — ТОЛЬКО из фактов, с [N] в конце утверждения. Не выдумывай цифр.
  • НЕ складывай разнотипные величины (разовую комиссию + годовую ставку = «APR»).
    ПСК/APR — только если ЯВНО в источнике.
  • НЕ дублируй сравнительную таблицу — она рендерится отдельно. Здесь — АНАЛИЗ.
  • Без маркетингового тона, без «возможно/можно предположить». Только из данных.
  • Без воды и повторов одного и того же разными словами.

ВЫХОД: чистый markdown отчёта (секции через ## / **жирный**), БЕЗ преамбулы,
БЕЗ markdown-fences вокруг всего ответа."""


def _facts_by_bank_block(ctx: NarrativeContext, max_per_bank: int = 22) -> str:
    """Факты, сгруппированные по банку (со слотами/условиями/цитатами)."""
    by_bank: dict[str, list[Fact]] = {}
    for f in ctx.facts:
        if f.attribute == "продукт_доступен":
            continue
        by_bank.setdefault(f.entity_bank_slug, []).append(f)
    name = {e.bank_slug: e.bank_name for e in ctx.entities}
    parts = []
    for e in ctx.entities:
        fs = by_bank.get(e.bank_slug, [])
        if not fs:
            parts.append(f"### {e.bank_name}: нет данных в открытых источниках")
            continue
        top = select_facts_for_section(fs, "", k=max_per_bank) or fs[:max_per_bank]
        parts.append(f"### {e.bank_name}\n" + format_facts_for_prompt(top, max_facts=max_per_bank))
    return "\n\n".join(parts)


def _deltas_lines(matrix, limit: int = 6) -> list[str]:
    """Крупнейшие числовые расхождения по банкам как список строк."""
    if matrix is None or not getattr(matrix, "variance", None):
        return []
    lines = []
    for attr, score in matrix.variance:
        if score <= 0:
            continue
        cells = [(e.bank_name, matrix.cell(e.bank_slug, attr)) for e in matrix.entities]
        vals = [(n, c) for n, c in cells
                if c is not None and getattr(c, "value_numeric", None) is not None]
        if len(vals) < 2:
            continue
        per = ", ".join(f"{n}: {c.value} {c.unit}".strip() for n, c in vals)
        lines.append(f"{attr.replace('_', ' ')}: {per}")
        if len(lines) >= limit:
            break
    return lines


def _deltas_block(matrix) -> str:
    """Крупнейшие числовые расхождения по банкам (для заземления выводов)."""
    lines = _deltas_lines(matrix)
    return ("# КЛЮЧЕВЫЕ РАСХОЖДЕНИЯ (раскрой крупнейшие в выводах)\n"
            + "\n".join(f"  • {ln}" for ln in lines) if lines else "")


def _synthesis_budget(n_banks: int) -> tuple[int, int]:
    """Бюджет входа под ширину запроса: чем больше банков, тем меньше фактов на
    банк и выдержек — иначе промпт раздувается и reasoning-вызов на rate-limited
    эндпоинте Foundation Models ловит таймаут (баг 5 банков → пустое тело).
    Возвращает (max_per_bank, excerpts_max_n)."""
    n = max(1, n_banks)
    max_per_bank = max(6, round(48 / n))     # 2б→24, 3б→16, 4б→12, 5б→10
    excerpts_n = max(3, 8 - n)               # 2б→6, 5б→3
    return max_per_bank, excerpts_n


def _deterministic_body(ctx: NarrativeContext, matrix) -> str:
    """Детерминированный аналитический body — СТРАХОВКА на случай, когда
    reasoning-синтез недоступен (таймаут/rate-limit эндпоинта). Лучше отдать
    аудитору заземлённую структуру из РЕАЛЬНЫХ фактов, чем пустое «нерелевантное»
    тело. Без LLM: дельты + разбор по банкам с цитатами.

    Помечается явно как авто-сборка, чтобы аудитор знал, что аналитический
    слой (выводы/рейтинг) собран без рассуждающей модели и требует внимания."""
    if not ctx.facts:
        return ""
    name = {e.bank_slug: e.bank_name for e in ctx.entities}
    by_bank: dict[str, list[Fact]] = {}
    for f in ctx.facts:
        if f.attribute == "продукт_доступен":
            continue
        by_bank.setdefault(f.entity_bank_slug, []).append(f)

    parts = ["## Анализ (авто-сборка из фактов)",
             "> ⚠ _Рассуждающая модель была недоступна (таймаут эндпоинта); "
             "аналитический слой собран детерминированно из извлечённых фактов. "
             "Выводы и рейтинг проверьте вручную по разбору ниже и таблице._", ""]

    deltas = _deltas_lines(matrix, limit=8)
    if deltas:
        parts.append("**Ключевые расхождения по банкам:**")
        parts.extend(f"- {ln}" for ln in deltas)
        parts.append("")

    parts.append("**Разбор по банкам:**")
    for e in ctx.entities:
        fs = by_bank.get(e.bank_slug, [])
        if not fs:
            parts.append(f"- **{e.bank_name}** — нет данных в открытых источниках.")
            continue
        top = select_facts_for_section(fs, "", k=8) or fs[:8]
        items = []
        for f in top:
            val = f"{f.value} {f.unit}".strip()
            cond = f" ({'; '.join(f.conditions)})" if f.conditions else ""
            cite = f" [{f.source_idx}]" if f.source_idx else ""
            items.append(f"{f.attribute.replace('_', ' ')}: {val}{cond}{cite}")
        parts.append(f"- **{e.bank_name}** — " + "; ".join(items) + ".")
    return "\n".join(parts)


def _build_user_msg(ctx: NarrativeContext, matrix, *, max_per_bank: int,
                    excerpts_n: int, with_excerpts: bool = True) -> str:
    """Сборка user-промпта под заданный бюджет (для масштабирования/ретрая)."""
    entities_str = ", ".join(e.bank_name for e in ctx.entities)
    brief_block = ctx.brief_block("")            # меморандум целиком
    deltas = _deltas_block(matrix)
    facts_block = _facts_by_bank_block(ctx, max_per_bank=max_per_bank)
    excerpts = ctx.excerpts_block(max_n=excerpts_n, per=500) if with_excerpts else ""
    return (
        (brief_block + "\n\n" if brief_block else "")
        + f"# ВОПРОС АУДИТОРА\n{ctx.question}\n\n"
        + f"# СРАВНИВАЕМЫЕ БАНКИ\n{entities_str}\n\n"
        + (deltas + "\n\n" if deltas else "")
        + f"# ФАКТЫ ПО БАНКАМ (slot = value | условия | [источник])\n{facts_block}\n\n"
        + (f"# ДОСЛОВНЫЕ ВЫДЕРЖКИ ИСТОЧНИКОВ\n{excerpts}\n\n" if excerpts else "")
        + "Напиши ОДИН связный аудиторский отчёт по структуре из системного промпта. "
        "Не дублируй сравнительную таблицу. Раскрой крупнейшие расхождения, дай рейтинг "
        "и конкретные риски/рекомендации с [N]."
    )


async def _call_synth(ctx, model, user_msg, *, timeout: int) -> str:
    resp = await asyncio.wait_for(
        ctx.client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_msg}],
            max_tokens=5000, temperature=0.0),
        timeout=timeout)
    return (resp.choices[0].message.content or "").strip()


async def generate_unified(ctx: NarrativeContext, matrix) -> str:
    """Один синтез-вызов → аналитический body отчёта (без сравнительной таблицы —
    она рендерится детерминированно отдельно). Возвращает markdown.

    Надёжность (баг 5 банков → пустое тело):
      • вход масштабируется под число банков (_synthesis_budget), чтобы промпт не
        раздувался и reasoning-вызов не ловил таймаут;
      • при таймауте/ошибке — РЕТРАЙ с урезанным входом (меньше фактов, без выдержек);
      • если и ретрай не прошёл — детерминированный аналитический фоллбэк (НИКОГДА
        не возвращаем пустую строку, иначе отчёт остаётся без тела)."""
    if not ctx.facts:
        return ""
    model = (os.getenv("LLM_MODEL_REASONING") or os.getenv("LLM_MODEL_SMART")
             or ctx.model or get_default_model())
    n_banks = len(ctx.entities)
    max_per_bank, excerpts_n = _synthesis_budget(n_banks)

    md = ""
    # Попытка 1 — полный (масштабированный) вход.
    try:
        user_msg = _build_user_msg(ctx, matrix, max_per_bank=max_per_bank,
                                   excerpts_n=excerpts_n)
        md = await _call_synth(ctx, model, user_msg, timeout=170)
    except Exception as e:
        log.warning("[unified_synthesis] attempt-1 failed (%d банков): %r — ретрай "
                    "с урезанным входом", n_banks, e)
        # Попытка 2 — половинный вход, без выдержек, короче таймаут.
        try:
            user_msg = _build_user_msg(ctx, matrix, max_per_bank=max(5, max_per_bank // 2),
                                       excerpts_n=2, with_excerpts=False)
            md = await _call_synth(ctx, model, user_msg, timeout=130)
        except Exception as e2:
            log.warning("[unified_synthesis] attempt-2 failed: %r — детерминированный фоллбэк", e2)

    if not md:
        fb = _deterministic_body(ctx, matrix)
        if fb:
            log.warning("[unified_synthesis] фоллбэк-body: %d символов (без LLM)", len(fb))
        return fb
    # Анти-галлюцинация (детерминированная сантехника — оставлена по требованию):
    #  • вычищаем несуществующие цитаты [N];
    #  • метим непроверенные номера ФЗ;
    #  • проверяем числа против фактов — при расхождении НЕ выбрасываем весь отчёт,
    #    а помечаем сноской (для аудита важно видеть, что число не сверено).
    allowed = {s.get("n") for s in ctx.sources_index if s.get("n")}
    md = enforce_citations(md, allowed, require_for_numbers=False)
    haystack = build_npa_haystack(ctx.facts, ctx.sources_index, ctx.question)
    md, unv_npa = annotate_unverified_npa(md, haystack)
    ok, halluc = verify_numbers_in_text(md, ctx.facts, strict=False)
    if not ok and halluc:
        log.warning("[unified_synthesis] непроверенные числа: %s", halluc[:8])
        md += ("\n\n> ⚠ _Числа, не сверенные с источниками автоматически: "
               + ", ".join(f"{h:g}" for h in halluc[:8])
               + " — проверьте по первоисточнику._")
    log.warning("[unified_synthesis] OK: %d символов, %d непроверенных чисел, %d НПА-флагов",
                 len(md), len(halluc), len(unv_npa))
    return md
