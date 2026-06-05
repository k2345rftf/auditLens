"""Government Programs — программы господдержки.

Применимо для:
  • Ипотека — семейная, военная, IT, сельская, для молодых учителей
  • Соц.карты — Соц.карта Москвича, ЕТК, пенсионные карты МИР
  • Маткапитал, единые пособия (СФР)
  • Кредиты — субсидированные ставки для МСП, фермеров

Использует факты + информацию из официальных источников (gosuslugi.ru, sfr.gov.ru).
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


PROGRAM_KEYWORDS = (
    "господдерж", "семейная", "военная", "сельская", "молод",
    "матерински", "сертификат", "субсиди", "льготн", "пособие",
    "соц.карт", "социальная карта", "пенсионн", "ветеран",
    "сфр", "пфр", "гос.услуги", "программа",
)


SYSTEM_PROMPT = """Ты — аудитор пишущий блок ПРОГРАММЫ ГОСПОДДЕРЖКИ.

Получаешь факты упоминающие государственные программы / льготы / субсидии.
Формируешь связный обзор:
  • Какие программы применимы к продукту
  • Кому положены (segment / requirement)
  • Какова разница в условиях для базового vs гос-варианта

ПРАВИЛА:

1) Конкретные ПРОГРАММЫ называй полностью:
   ✅ «Программа "Семейная ипотека" ФЗ-256»
   ❌ «Господдержка ипотеки»

2) Цифры — только из фактов.

3) Каждое утверждение → [N].

ВЫХОД: JSON:
{
  "narrative": "1-2 абзаца",
  "programs": [
    {"name": "Семейная ипотека", "eligibility": "Кому доступна", "benefit": "Что даёт", "source_idx": 5}
  ]
}

БЕЗ преамбулы."""


def _has_program_signal(f: Fact) -> bool:
    text = (f.attribute + " " + f.value + " " + f.verbatim_quote + " " +
              f.qualifications + " " + " ".join(f.conditions)).lower()
    return any(kw in text for kw in PROGRAM_KEYWORDS)


async def generate(ctx: NarrativeContext) -> str:
    prog_facts = [f for f in ctx.facts if _has_program_signal(f)]
    if not prog_facts:
        return ""

    facts_str = format_facts_for_prompt(prog_facts, max_facts=25)
    user_msg = (
        f"# Тема\n{ctx.question}\n\n"
        f"# Факты упоминающие гос. программы ({len(prog_facts)})\n{facts_str}\n\n"
        f"Сформулируй блок программ господдержки. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(prog_facts)

    data = parse_json_object(raw) or {}
    narrative = str(data.get("narrative") or "").strip()
    programs = data.get("programs") or []

    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    if narrative:
        ok, _ = verify_numbers_in_text(narrative, prog_facts)
        if ok:
            narrative = enforce_citations(narrative, allowed_src,
                                             require_for_numbers=True)
        else:
            narrative = ""

    clean_progs = []
    for p in programs[:6]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        clean_progs.append({
            "name": name,
            "eligibility": str(p.get("eligibility") or "").strip(),
            "benefit": str(p.get("benefit") or "").strip(),
            "source_idx": p.get("source_idx"),
        })

    parts = ["## 🏛 Программы господдержки", ""]
    if narrative:
        parts.append(narrative)
        parts.append("")
    for p in clean_progs:
        cite = f" [{p['source_idx']}]" if p['source_idx'] else ""
        parts.append(f"**{p['name']}**{cite}")
        if p['eligibility']:
            parts.append(f"  _Доступна:_ {p['eligibility']}")
        if p['benefit']:
            parts.append(f"  _Даёт:_ {p['benefit']}")
        parts.append("")
    return "\n".join(parts).rstrip()


def _fallback(facts: list[Fact]) -> str:
    parts = ["## 🏛 Программы господдержки", ""]
    for f in facts[:5]:
        cite = f" [{f.source_idx}]" if f.source_idx else ""
        parts.append(f"- {f.attribute}: {f.value} {f.unit}{cite}".strip())
        if f.verbatim_quote:
            parts.append(f"  > {f.verbatim_quote[:200]}")
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
        log.warning("[government_programs] LLM failed: %s", e)
        return ""
