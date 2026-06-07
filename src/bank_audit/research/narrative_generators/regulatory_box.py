"""Regulatory Box — НПА / регулятор / закон.

Цель: дать аудитору информацию о ЮРИДИЧЕСКОЙ ОСНОВЕ продукта:
ссылки на ГК РФ, законы, инструкции ЦБ, информ-письма.

Применимо для тем: доверенности (ГК РФ ст.185-189), ипотека (ФЗ-102),
вклады (ФЗ-177 о страховании), эквайринг (115-ФЗ),
военная ипотека (117-ФЗ), маткапитал (256-ФЗ).

ПРИМЕР качественного regulatory box (из demo/doverennost.json):
  «Доверенности на банковские операции регулируются ст.185-189 ГК РФ [12].
   Срок действия — до 3 лет, при отсутствии указания срока — 1 год (ст.186).
   Нотариальное удостоверение обязательно для распоряжения недвижимостью
   или денежными средствами свыше определённой суммы (информационное
   письмо ЦБ от 02.2024) [13]. Банки обязаны проверять подлинность
   доверенности через ЕИС нотариата [14].»

Использует только facts с category="regulation" + sources с trust_score>=0.9
из официальных доменов (cbr.ru, pravo.gov.ru, consultant.ru, mil.ru).
"""
from __future__ import annotations
import asyncio, logging
from openai import AsyncOpenAI

from .base import (
    NarrativeContext,
    parse_json_object,
    verify_numbers_in_text,
    enforce_citations,
    format_facts_for_prompt,
    facts_by_category,
    get_default_model,
)
from ..fact import Fact

log = logging.getLogger(__name__)


REGULATORY_DOMAINS = {
    "cbr.ru", "consultant.ru", "pravo.gov.ru", "mil.ru", "mgnp.info",
    "fas.gov.ru", "minfin.gov.ru", "garant.ru", "sudact.ru", "nalog.gov.ru",
    "gosuslugi.ru", "kremlin.ru", "duma.gov.ru",
}


SYSTEM_PROMPT = """Ты — корпоративный юрист пишущий регуляторную справку
для банковского аудитора.

Получаешь регуляторные факты (категория regulation) и официальные источники.
Пишешь 1-2 абзаца с КОНКРЕТНЫМИ ссылками на статьи закона / номера документов.

ПРАВИЛА:

0) ⛔ НЕ ВЫДУМЫВАЙ НОМЕРА ЗАКОНОВ. Указывай номер ФЗ/постановления ТОЛЬКО если
   он ДОСЛОВНО присутствует в переданных фактах/источниках. Если точного номера
   нет — опиши норму СЛОВАМИ без номера («по закону о потребительском кредите»,
   «согласно правилам страхования вкладов»), но НЕ придумывай ни номер, ни название.
   Неверный номер закона хуже, чем его отсутствие. Не путай номер и название
   (напр. ФЗ-102 — «Об ипотеке», а НЕ «о банках»).

1) ССЫЛКА НА КОНКРЕТНУЮ СТАТЬЮ — где она ЕСТЬ в источнике:
   ✅ «ст. 185 ГК РФ» / «п. 2 ст. 837 ГК РФ» / «ФЗ-177 ст. 11»
   ❌ «согласно ГК РФ» — слишком общо

2) ДАТЫ — конкретные:
   ✅ «информационное письмо ЦБ от 02.2024 №ИН-08-12/8»
   ❌ «недавнее письмо ЦБ»

3) ЦИФРЫ — из фактов:
   ✅ «срок до 3 лет (ст. 186 ГК РФ)»
   ❌ «срок до нескольких лет»

4) КАЖДОЕ УТВЕРЖДЕНИЕ → [N] цитата на источник.

5) ИЗБЕГАЙ:
   ❌ Юридических рассуждений «суд может решить...»
   ❌ Прогнозов изменений
   ❌ Общих фраз про правовую систему

ВЫХОД: JSON:
{
  "narrative": "1-2 абзаца с цитатами на конкретные статьи [N]",
  "citations": [
    {"reference": "ст. 185 ГК РФ", "source_idx": 12, "note": "общие положения о доверенности"},
    ...
  ]
}

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext) -> str:
    """Главная."""
    reg_facts = facts_by_category(ctx.facts, ["regulation"])
    # Также подходят высокоприоритетные факты со ссылками на нпа в verbatim
    extra = [f for f in ctx.facts
              if f.audit_priority == "high"
              and any(kw in f.verbatim_quote.lower() for kw in
                       ("гк рф", "фз", "цб", "ст.", "ст ", "пункт", "ст:",
                        "регулят", "закон"))]
    reg_facts = list({(f.entity_bank_slug, f.attribute): f
                       for f in reg_facts + extra}.values())

    # Если есть официальные источники — используем их даже без regulation-facts
    reg_sources = [s for s in ctx.sources_index
                    if s.get("domain", "") in REGULATORY_DOMAINS
                    or s.get("trust_score", 0) >= 0.95]

    if not reg_facts and not reg_sources:
        return ""   # секции не будет

    facts_str = format_facts_for_prompt(reg_facts, max_facts=15) if reg_facts else "(нет regulation-фактов)"
    sources_str = _format_reg_sources(reg_sources)

    user_msg = (
        f"# Тема\n{ctx.question}\n\n"
        f"# Регуляторные факты\n{facts_str}\n\n"
        f"# Официальные источники\n{sources_str}\n\n"
        f"Напиши регуляторную справку 1-2 абзаца с конкретными статьями. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(reg_facts, reg_sources)

    data = parse_json_object(raw) or {}
    narrative = str(data.get("narrative") or "").strip()
    citations = data.get("citations") or []
    if not isinstance(citations, list):
        citations = []

    if narrative:
        allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
        ok, halluc = verify_numbers_in_text(narrative, reg_facts + [_dummy_fact(s) for s in reg_sources])
        # Для regulatory более либеральны к числам (даты, номера ФЗ)
        # — оставляем narrative даже если halluc, но enforced citations
        narrative = enforce_citations(narrative, allowed_src, require_for_numbers=True)

    return _render_md(narrative, citations, reg_sources)


def _dummy_fact(source: dict) -> Fact:
    """Превращает source в синтетический Fact для verify_numbers."""
    return Fact(
        entity_bank_slug="", attribute="", value="",
        verbatim_quote=" ".join(source.get("excerpts", []))[:600],
    )


def _format_reg_sources(srcs: list[dict]) -> str:
    if not srcs:
        return "(нет официальных источников)"
    lines = []
    for s in srcs[:10]:
        n = s.get("n")
        title = (s.get("title") or "")[:120]
        domain = s.get("domain", "")
        excerpt = " ".join(s.get("excerpts", []))[:400]
        lines.append(f"[{n}] {title} ({domain})\n    {excerpt}")
    return "\n".join(lines)


def _render_md(narrative: str, citations: list[dict],
                 sources: list[dict]) -> str:
    parts = ["## 📜 Регуляторное поле", ""]
    if narrative:
        parts.append(narrative)
        parts.append("")
    if citations:
        parts.append("**Применимые НПА:**")
        parts.append("")
        for c in citations[:8]:
            if not isinstance(c, dict):
                continue
            ref = str(c.get("reference") or "").strip()
            note = str(c.get("note") or "").strip()
            src = c.get("source_idx")
            cite = f" [{src}]" if src else ""
            if ref:
                line = f"- **{ref}**{cite}"
                if note:
                    line += f" — {note}"
                parts.append(line)
    return "\n".join(parts).rstrip()


def _fallback(facts: list[Fact], sources: list[dict]) -> str:
    """Без LLM — просто список источников."""
    parts = ["## 📜 Регуляторное поле", ""]
    parts.append("_LLM-narrative недоступен. Релевантные официальные источники:_")
    parts.append("")
    for s in sources[:6]:
        n = s.get("n")
        title = (s.get("title") or "")[:100]
        url = s.get("url", "")
        parts.append(f"- [{n}] [{title}]({url})")
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
        log.warning("[regulatory_box] LLM failed: %s", e)
        return ""
