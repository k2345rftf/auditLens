"""Conflict Explainer — интерпретация расхождений в источниках.

Когда matrix_builder выявил конфликт (разные значения одного атрибута
у одного банка из РАЗНЫХ источников), важно объяснить аудитору ПОЧЕМУ
так получилось:

  • Источники датированы разными периодами (тариф изменился)
  • Один источник про сегмент Premium, другой про массовый
  • Один источник — bank.ru, другой — sravni.ru (агрегатор устарел)
  • Один источник — РФ-резидент, другой — нерезидент

ПРИМЕР:
  «У Сбера годовое обслуживание указано как 0 ₽ [1] и 990 ₽ [5].
   Различие объясняется тем что [1] показывает условие для активных
   клиентов с зачислением пенсии, а [5] — базовый тариф без условий.
   Аудитору следует уточнить, какой сегмент рассматривается.»
"""
from __future__ import annotations
import asyncio, logging
from openai import AsyncOpenAI

from .base import (
    NarrativeContext,
    parse_json_object,
    enforce_citations,
    get_default_model,
)
from ..fact import Fact

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аудитор анализирующий КОНФЛИКТЫ в источниках.

Получаешь список конфликтов (банк × атрибут → несколько разных значений
из разных источников). Объясняешь каждый конфликт — ПОЧЕМУ источники
расходятся, и что аудитору с этим делать.

ТИПИЧНЫЕ ПРИЧИНЫ:
  • Разные даты публикации (тариф изменился)
  • Разные сегменты клиентов (массовый vs Premium)
  • Разные регионы / валюты
  • Разные продуктовые версии
  • Агрегатор устарел / банк опубликовал акцию
  • Опечатка / неправильная интерпретация банком

ПРАВИЛА:

1) Не утверждай ОДНОЗНАЧНО что источник «врёт». Формулируй гипотезы:
   ✅ «Расхождение может объясняться разными сегментами»
   ✅ «Источник 5 датирован 2023, а источник 1 — 2024 — тариф изменился»
   ❌ «Источник 5 устарел»  — без подтверждения

2) Каждое объяснение → [N] цитата.

3) Реkomенда: «Аудитор должен запросить у банка официальное подтверждение».

ВЫХОД: JSON:
{
  "conflicts": [
    {
      "bank_name": "Сбер",
      "attribute": "годовое_обслуживание",
      "values": "0 ₽ [1] vs 990 ₽ [5]",
      "explanation": "Расхождение может объясняться... [1, 5]",
      "audit_action": "Запросить у банка актуальную тарифную ведомость"
    }
  ]
}

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext, conflicts: dict) -> str:
    """Главная.

    conflicts: dict[tuple[bank_slug, attr], list[Fact|Triple]]
    """
    if not conflicts:
        return ""

    conflict_lines = []
    for (bank, attr), group in list(conflicts.items())[:8]:
        bank_name = next((e.bank_name for e in ctx.entities
                           if e.bank_slug == bank), bank)
        vals = []
        for g in group:
            val = f"{g.value} {g.unit}".strip()
            src = getattr(g, "source_idx", 0)
            cite = f" [{src}]" if src else ""
            # Цитата если есть
            verb = getattr(g, "verbatim_quote", None) or getattr(g, "excerpt", "")
            vals.append(f"{val}{cite}" + (f" — «{verb[:100]}»" if verb else ""))
        conflict_lines.append(f"## {bank_name} × {attr}\n" + "\n".join(f"  • {v}" for v in vals))

    user_msg = (
        f"# Вопрос аудитора\n{ctx.question}\n\n"
        f"# Найденные конфликты\n\n" + "\n\n".join(conflict_lines) + "\n\n"
        f"Объясни каждый конфликт. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(ctx, conflicts)

    data = parse_json_object(raw) or {}
    items = data.get("conflicts") or []
    if not isinstance(items, list):
        items = []

    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        explanation = enforce_citations(
            str(it.get("explanation") or "").strip(),
            allowed_src, require_for_numbers=False)
        clean.append({
            "bank_name": str(it.get("bank_name") or "").strip(),
            "attribute": str(it.get("attribute") or "").strip(),
            "values": str(it.get("values") or "").strip(),
            "explanation": explanation,
            "audit_action": str(it.get("audit_action") or "").strip(),
        })

    if not clean:
        return _fallback(ctx, conflicts)

    parts = ["## ⚠️ Расхождения в источниках", ""]
    for c in clean:
        parts.append(f"### {c['bank_name']} — {c['attribute']}")
        parts.append(f"**Расхождение:** {c['values']}")
        if c['explanation']:
            parts.append("")
            parts.append(c['explanation'])
        if c['audit_action']:
            parts.append("")
            parts.append(f"**Действие:** {c['audit_action']}")
        parts.append("")
    return "\n".join(parts).rstrip()


def _fallback(ctx: NarrativeContext, conflicts: dict) -> str:
    parts = ["## ⚠️ Расхождения в источниках", ""]
    for (bank, attr), group in list(conflicts.items())[:8]:
        bank_name = next((e.bank_name for e in ctx.entities
                           if e.bank_slug == bank), bank)
        parts.append(f"### {bank_name} — {attr}")
        for g in group:
            val = f"{g.value} {g.unit}".strip()
            src = getattr(g, "source_idx", 0)
            cite = f" [{src}]" if src else ""
            parts.append(f"- {val}{cite}")
        parts.append("")
    return "\n".join(parts)


async def _llm_call(ctx: NarrativeContext, user_msg: str) -> str:
    try:
        resp = await asyncio.wait_for(
            ctx.client.chat.completions.create(
                model=ctx.model or get_default_model(),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=1500, temperature=0.0,
            ),
            timeout=120,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[conflict_explainer] LLM failed: %s", e)
        return ""
