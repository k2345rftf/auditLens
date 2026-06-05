"""Requirements Box — требования к клиенту (документы, возраст, доход).

Применимо для:
  • Ипотека — паспорт, СНИЛС, 2-НДФЛ, ПТС
  • Социальная карта — пенсионное удостоверение / справка из СФР
  • Кредит — справка о доходе, кредитная история
  • Военная ипотека — рапорт командира части
  • Маткапитал — паспорт мамы + сертификат СФР
"""
from __future__ import annotations
import asyncio, logging
from openai import AsyncOpenAI

from .base import (
    NarrativeContext,
    parse_json_object,
    enforce_citations,
    verify_numbers_in_text,
    format_facts_for_prompt,
    facts_by_category,
    get_default_model,
)
from ..fact import Fact

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аудитор пишущий блок ТРЕБОВАНИЙ К КЛИЕНТУ.

На основе фактов категории requirement и других фактов где упоминаются
документы/возраст/доход, ты составляешь чёткий чеклист.

ПРАВИЛА:

1) ГРУППИРУЙ по типу требования:
   • Документы (паспорт, СНИЛС, справки)
   • Возраст (от X до Y лет)
   • Доход / занятость
   • Гражданство / регистрация
   • Специальный статус (пенсионер, военнослужащий, льготник)

2) ОТМЕЧАЙ если требования разнятся между банками:
   ✅ «У Сбера достаточно паспорта, у ВТБ требуется СНИЛС [1, 3]»

3) ЧИСЛА И ВОЗРАСТЫ — только из фактов:
   ✅ «Минимальный возраст: 18 (Сбер), 21 (ВТБ) [1, 2]»

4) КАЖДОЕ УТВЕРЖДЕНИЕ → [N] цитата.

ВЫХОД: JSON:
{
  "intro": "1 предложение",
  "groups": [
    {
      "group_name": "Документы",
      "items": [
        {"text": "Паспорт РФ (все банки) [1, 2, 3]"},
        ...
      ]
    },
    ...
  ]
}

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext) -> str:
    req_facts = facts_by_category(ctx.facts, ["requirement"])
    if not req_facts:
        return ""

    facts_str = format_facts_for_prompt(req_facts, max_facts=30)
    entities_str = ", ".join(e.bank_name for e in ctx.entities)

    user_msg = (
        f"# Сравниваемые банки\n{entities_str}\n\n"
        f"# Факты-требования ({len(req_facts)})\n{facts_str}\n\n"
        f"Сформулируй блок требований с [N]. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(req_facts)

    data = parse_json_object(raw) or {}
    groups = data.get("groups") or []
    intro = str(data.get("intro") or "").strip()

    if not isinstance(groups, list) or not groups:
        return _fallback(req_facts)

    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    clean_groups = []
    for g in groups[:6]:
        if not isinstance(g, dict):
            continue
        gname = str(g.get("group_name") or "").strip()
        items = g.get("items") or []
        if not isinstance(items, list) or not gname:
            continue
        clean_items = []
        for it in items[:8]:
            if not isinstance(it, dict):
                continue
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            ok, _ = verify_numbers_in_text(text, req_facts)
            if not ok:
                continue
            text = enforce_citations(text, allowed_src, require_for_numbers=True)
            clean_items.append(text)
        if clean_items:
            clean_groups.append({"group_name": gname, "items": clean_items})

    if not clean_groups:
        return _fallback(req_facts)

    return _render_md(intro, clean_groups)


def _render_md(intro: str, groups: list[dict]) -> str:
    parts = ["## 📋 Требования к клиенту", ""]
    if intro:
        parts.append(intro)
        parts.append("")
    for g in groups:
        parts.append(f"**{g['group_name']}:**")
        for item in g['items']:
            parts.append(f"- {item}")
        parts.append("")
    return "\n".join(parts).rstrip()


def _fallback(facts: list[Fact]) -> str:
    parts = ["## 📋 Требования к клиенту", ""]
    for f in facts[:10]:
        cite = f" [{f.source_idx}]" if f.source_idx else ""
        parts.append(f"- **{f.attribute}**: {f.value} {f.unit}{cite}".strip())
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
                max_tokens=1200, temperature=0.0,
            ),
            timeout=40,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[requirements_box] LLM failed: %s", e)
        return ""
