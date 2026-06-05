"""Cant-Do Box — что НЕЛЬЗЯ делать по продукту (negative list).

Применимо для:
  • Доверенности — нельзя закрывать счёт, нельзя снять > X ₽
  • Эквайринг — нельзя для определённых MCC, нельзя без интернета
  • Вклады — нельзя пополнение в первые N дней
  • Кредиты — нельзя расходование на определённые цели
  • Карты — нельзя за границей (для соц./пенс. карт), нельзя в casino

Источник данных: exceptions[], qualifications, отрицательные verbatim_quotes
(содержат «нельзя», «запрещ», «ограничено», «не предусмотрено», «не доступно»).

ПРИМЕР качественного cant_do (из demo/doverennost.json):
  По доверенности НЕЛЬЗЯ:
  - Закрывать вклад/счёт — требуется присутствие владельца [1]
  - Снимать наличные единовременно более 100 000 ₽ [4]
  - Распоряжаться депозитарным/брокерским счётом [5]
  - Совершать валютообменные операции на >10 000 USD без личного присутствия [3]
"""
from __future__ import annotations
import asyncio, logging, re
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


NEGATIVE_KEYWORDS = (
    "нельзя", "запрещ", "ограничен", "не предусмотрен", "не доступн",
    "не позволя", "недоступн", "невозможн", "не распростран",
    "исключен", "за исключением", "не применя", "блокир",
)


def _has_negative_signal(f: Fact) -> bool:
    """Эвристика: факт содержит запрет/ограничение."""
    if f.exceptions:
        return True
    text = (f.value + " " + f.verbatim_quote + " " +
              " ".join(f.conditions) + " " + f.qualifications).lower()
    return any(kw in text for kw in NEGATIVE_KEYWORDS)


SYSTEM_PROMPT = """Ты — аудитор-комплайенс пишущий блок ОГРАНИЧЕНИЙ.

На основе фактов с ограничениями/исключениями ты формулируешь список того
ЧТО ПО ЭТОМУ ПРОДУКТУ НЕЛЬЗЯ.

ПРАВИЛА:

1) Каждая позиция — КОНКРЕТНОЕ действие которое запрещено или ограничено.
   ✅ «Снять >100 000 ₽ единовременно без присутствия владельца [4]»
   ❌ «Есть ограничения»  — расплывчато

2) Если ограничение касается определённого банка — указывай банк:
   ✅ «У ВТБ: переводы доступны только клиентам Привилегия+ [3]»

3) Каждое утверждение → [N] цитата.

4) НЕ дублируй между банками — если у всех ограничение «нельзя снимать
   >100k», пиши «у всех банков» один раз.

5) НЕ ВЫДУМЫВАЙ ограничения которых нет в фактах.

ВЫХОД: JSON:
{
  "intro": "1 предложение про общий характер ограничений",
  "restrictions": [
    {
      "action": "Что нельзя (конкретно)",
      "applies_to": "Кому/чему применяется (банки/сегменты)",
      "source_idx": 4,
      "severity": "blocking|conditional|admin"
    },
    ...
  ]
}

severity:
  • blocking    — действие полностью невозможно
  • conditional — возможно при дополнительных условиях
  • admin       — административные нюансы (требуется заявление, очное и т.п.)

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext) -> str:
    """Главная."""
    neg_facts = [f for f in ctx.facts if _has_negative_signal(f)]
    if not neg_facts:
        return ""

    facts_str = format_facts_for_prompt(neg_facts, max_facts=30)
    entities_str = ", ".join(e.bank_name for e in ctx.entities)

    user_msg = (
        f"# Тема\n{ctx.question}\n\n"
        f"# Сравниваемые банки\n{entities_str}\n\n"
        f"# Факты с ограничениями/исключениями ({len(neg_facts)})\n{facts_str}\n\n"
        f"Сформулируй список ограничений (что НЕЛЬЗЯ) с [N]. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(neg_facts)

    data = parse_json_object(raw) or {}
    restrictions = data.get("restrictions") or []
    intro = str(data.get("intro") or "").strip()
    if not isinstance(restrictions, list):
        restrictions = []

    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    clean = []
    for r in restrictions[:12]:
        if not isinstance(r, dict):
            continue
        action = str(r.get("action") or "").strip()
        if not action:
            continue
        # Verify
        ok, _ = verify_numbers_in_text(action, neg_facts)
        if not ok:
            continue
        action = enforce_citations(action, allowed_src, require_for_numbers=True)
        clean.append({
            "action": action,
            "applies_to": str(r.get("applies_to") or "").strip(),
            "source_idx": r.get("source_idx"),
            "severity": str(r.get("severity") or "conditional").lower(),
        })

    if not clean:
        return _fallback(neg_facts)

    return _render_md(intro, clean)


def _render_md(intro: str, restrictions: list[dict]) -> str:
    parts = ["## 🚫 Что НЕЛЬЗЯ по данному продукту", ""]
    if intro:
        parts.append(intro)
        parts.append("")
    sev_emoji = {"blocking": "🔴", "conditional": "🟡", "admin": "🔵"}
    # Group by severity
    grouped: dict[str, list[dict]] = {}
    for r in restrictions:
        grouped.setdefault(r["severity"], []).append(r)
    for sev in ["blocking", "conditional", "admin"]:
        if sev not in grouped:
            continue
        emoji = sev_emoji.get(sev, "•")
        label = {"blocking": "Категорически нельзя",
                  "conditional": "Возможно при условиях",
                  "admin": "Административные требования"}.get(sev, sev)
        parts.append(f"**{emoji} {label}:**")
        parts.append("")
        for r in grouped[sev]:
            line = f"- {r['action']}"
            if r['applies_to']:
                line += f" _(применяется: {r['applies_to']})_"
            parts.append(line)
        parts.append("")
    return "\n".join(parts).rstrip()


def _fallback(neg_facts: list[Fact]) -> str:
    parts = ["## 🚫 Что НЕЛЬЗЯ по данному продукту", ""]
    parts.append("_LLM-narrative не сформирован. Сырые ограничения из фактов:_")
    parts.append("")
    for f in neg_facts[:10]:
        cite = f" [{f.source_idx}]" if f.source_idx else ""
        if f.exceptions:
            for exc in f.exceptions[:2]:
                parts.append(f"- {exc} _(к: {f.attribute})_{cite}")
        elif f.verbatim_quote:
            parts.append(f"- {f.verbatim_quote[:200]}{cite}")
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
            timeout=45,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[cant_do_box] LLM failed: %s", e)
        return ""
