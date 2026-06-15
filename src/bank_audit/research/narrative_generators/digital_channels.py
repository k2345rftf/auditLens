"""Digital Channels — сравнение дистанционных сервисов.

Применимо когда среди фактов есть упоминания мобильного приложения,
онлайн-банкинга, SMS-уведомлений, push-нотификаций, биометрии.

Цель: одна табличка-сравнение + краткий narrative «у кого что есть».
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
    get_default_model,
)
from ..fact import Fact

log = logging.getLogger(__name__)


DIGITAL_KEYWORDS = (
    "приложен", "онлайн", "интернет-банк", "мобильн", "дистанц",
    "push", "уведомлен", "ios", "android", "биометри", "selfie",
    "видеозвонок", "qr", "sbp", "сбп", "удалённ", "самосто",
)


def _is_digital_fact(f: Fact) -> bool:
    """Эвристика: факт о дистанционном сервисе."""
    text = (f.attribute + " " + f.value + " " + f.verbatim_quote).lower()
    return any(kw in text for kw in DIGITAL_KEYWORDS)


SYSTEM_PROMPT = """Ты — аудитор пишущий блок ДИСТАНЦИОННЫЕ СЕРВИСЫ.

На основе фактов о мобильных приложениях, онлайн-банкинге, удалённых
операциях составляешь сравнение возможностей по банкам.

ПРАВИЛА:

1) Структурируй по типу канала:
   • Мобильное приложение (iOS / Android)
   • Веб-кабинет
   • Удалённое оформление
   • Уведомления (SMS / push)

2) Указывай ОТСУТСТВИЕ функции если у одного банка есть, у другого нет:
   ✅ «Биометрический вход — есть у Сбер [1], нет данных у ВТБ»

3) Каждое утверждение → [N] цитата.

4) НЕ ВЫДУМЫВАЙ функций — только то что в фактах.

ВЫХОД: JSON:
{
  "narrative": "1 абзац (3-5 предложений) про дистанционные возможности",
  "comparison": [
    {"capability": "Мобильное приложение", "per_bank": {"sber": "iOS/Android [1]", "vtb": "iOS/Android [2]"}}
  ]
}

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext) -> str:
    from .base import box_gate
    digital_facts = [f for f in ctx.facts if _is_digital_fact(f)]
    # Гейт: не плодим секцию из одного-двух разрозненных упоминаний.
    if not box_gate(digital_facts, ctx.entities, min_facts=2, require_multi_bank=True):
        return ""

    facts_str = format_facts_for_prompt(digital_facts, max_facts=25)
    entities_str = ", ".join(f"{e.bank_name} ({e.bank_slug})" for e in ctx.entities)

    user_msg = (
        f"# Сравниваемые банки\n{entities_str}\n\n"
        f"# Факты о дистанционных сервисах ({len(digital_facts)})\n{facts_str}\n\n"
        f"Напиши narrative + comparison. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(ctx, digital_facts)

    data = parse_json_object(raw) or {}
    narrative = str(data.get("narrative") or "").strip()
    comparison = data.get("comparison") or []
    if not isinstance(comparison, list):
        comparison = []

    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    if narrative:
        ok, _ = verify_numbers_in_text(narrative, digital_facts)
        if not ok:
            narrative = ""
        else:
            narrative = enforce_citations(narrative, allowed_src,
                                             require_for_numbers=True)

    return _render_md(ctx, narrative, comparison)


def _render_md(ctx: NarrativeContext, narrative: str, comparison: list[dict]) -> str:
    parts = ["## 📱 Дистанционные сервисы", ""]
    if narrative:
        parts.append(narrative)
        parts.append("")
    if comparison:
        banks = [(e.bank_slug, e.bank_name) for e in ctx.entities]
        header = "| Возможность | " + " | ".join(b[1] for b in banks) + " |"
        sep    = "|---" + ("|---" * len(banks)) + "|"
        parts.append(header)
        parts.append(sep)
        for c in comparison[:10]:
            if not isinstance(c, dict):
                continue
            cap = str(c.get("capability") or "").strip()
            per_bank = c.get("per_bank") or {}
            if not cap or not isinstance(per_bank, dict):
                continue
            row = [cap]
            for slug, _ in banks:
                row.append(str(per_bank.get(slug) or "—"))
            parts.append("| " + " | ".join(row) + " |")
    return "\n".join(parts).rstrip()


def _fallback(ctx: NarrativeContext, facts: list[Fact]) -> str:
    parts = ["## 📱 Дистанционные сервисы", ""]
    for f in facts[:8]:
        bank = next((e.bank_name for e in ctx.entities
                       if e.bank_slug == f.entity_bank_slug), f.entity_bank_slug)
        cite = f" [{f.source_idx}]" if f.source_idx else ""
        parts.append(f"- **{bank}** — {f.attribute}: {f.value} {f.unit}{cite}".strip())
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
            timeout=120,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[digital_channels] LLM failed: %s", e)
        return ""
