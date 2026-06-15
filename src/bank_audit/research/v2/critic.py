"""Critic — верификатор отчёта.

Отдельный вызов (не тот же, что Analyst — конфликт интересов). Проверяет:
  1. Числовая верификация: каждое число в отчёте ↔ есть в bundle.facts.
  2. Claim-grounding: сильные выводы подтверждены фактами/дельтами.
  3. Coverage: отчёт отвечает на все части вопроса.
  4. Пустоты/вода: есть ли места без опоры.

Если critic находит проблемы → orchestrator просит Analyst переписать с
конкретными замечаниями (одна итерация).

Переиспользует anti-hallucination guards из narrative_generators/base.py
(verify_numbers, NPA-проверка) — детерминированная сантехника.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from .knowledge_bundle import KnowledgeBundle, Fact

log = logging.getLogger(__name__)


@dataclass
class Critique:
    ok: bool
    blocking_issues: list[str] = field(default_factory=list)
    weak_claims: list[str] = field(default_factory=list)
    missing_aspects: list[str] = field(default_factory=list)  # части вопроса без ответа
    numeric_hallucinations: list[float] = field(default_factory=list)
    repair_directive: str = ""  # инструкция для переписывания


SYSTEM_PROMPT = """Ты — критик аудиторских отчётов. Твоя задача — проверить
ЧЕРНОВИК отчёта на качество и достоверность, опираясь на KNOWLEDGE BUNDLE.

Проверяешь 4 аспекта:

1. ОТВЕЧАЕТ ЛИ НА ВОПРОС: разбей вопрос аудитора на части. Каждая часть
   должна быть освещена. Если что-то пропущено (напр. просили рейтинг — нет
   рейтинга) — это blocking_issue.

2. CLAIM-GROUNDING: каждое сильное утверждение («Сбер дороже», «Т-Банк
   надёжнее») должно опираться на конкретные факты из bundle. Голословные
   выводы → weak_claims.

3. ЧИСЛОВАЯ ДОСТОВЕРНОСТЬ: числа в отчёте должны быть из bundle (с тем же
   значением). Если число выдумано или искажено → numeric_hallucinations.

4. ПУСТОТЫ/ВОДА: абзацы без фактической опоры, повторы, маркетинговый тон.
   → weak_claims.

ВЫХОД (строгий JSON):
{
  "ok": false,                      // true только если серьёзных проблем нет
  "blocking_issues": ["Нет рейтинга, хотя аудитор просил"],
  "weak_claims": ["«Сбер надёжнее» — голословно, нет опоры"],
  "missing_aspects": ["рейтинг"],
  "numeric_hallucinations": [],
  "repair_directive": "Добавь рейтинг-таблицу (он есть в bundle). Замени голословное утверждение на «Сбер дороже на 1,5% [3]». Убери маркетинговый абзац про «удобство»."
}

Если отчёт хороший — верни {"ok":true,"blocking_issues":[],...} с пустым repair_directive.
"""


async def critique_report(client: AsyncOpenAI, report_md: str,
                            bundle: KnowledgeBundle, question: str,
                            model: str | None = None) -> Critique:
    """Верифицирует отчёт. Возвращает Critique с замечаниями."""
    if len(report_md) < 200:
        return Critique(ok=False, blocking_issues=["Отчёт слишком короткий / пустой"])

    model = model or os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                                 "gpt-4o-mini")

    # Сначала детерминированная проверка чисел (быстро, без LLM)
    halluc_nums = _check_numbers(report_md, bundle)

    context = bundle.to_prompt_context(max_chars=14000)
    user_msg = (
        f"# ВОПРОС АУДИТОРА\n{question}\n\n"
        f"# ЧЕРНОВИК ОТЧЁТА\n{report_md[:12000]}\n\n"
        f"# KNOWLEDGE BUNDLE\n{context}\n\n"
        f"Проверь отчёт. JSON."
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=1500,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[critic] LLM failed: %s — only deterministic check", e)
        return Critique(ok=len(halluc_nums) == 0,
                          numeric_hallucinations=halluc_nums)

    data = _parse_json(raw)
    if not data:
        return Critique(ok=len(halluc_nums) == 0,
                          numeric_hallucinations=halluc_nums)

    # Дополняем LLM-проверку детерминированными находками
    llm_halluc = [float(x) for x in (data.get("numeric_hallucinations") or [])
                    if _is_number(x)]
    all_halluc = list(set(halluc_nums + llm_halluc))

    return Critique(
        ok=bool(data.get("ok")) and len(all_halluc) == 0,
        blocking_issues=[str(x) for x in (data.get("blocking_issues") or [])][:6],
        weak_claims=[str(x) for x in (data.get("weak_claims") or [])][:8],
        missing_aspects=[str(x) for x in (data.get("missing_aspects") or [])][:5],
        numeric_hallucinations=all_halluc[:10],
        repair_directive=str(data.get("repair_directive") or ""),
    )


# ════════════════════════════════════════════════════════════════════════
# Детерминированная проверка чисел (переиспользует base.py guards)
# ════════════════════════════════════════════════════════════════════════


def _check_numbers(report_md: str, bundle: KnowledgeBundle) -> list[float]:
    """Извлекает числа из отчёта, сверяет с фактами. Возвращает галлюцинации.

    Строго: только числа С ЕДИНИЦЕЙ (₽, %, лет), в безопасных диапазонах
    (годы 1990-2050, малые 1-100) — пропускаем.
    """
    try:
        from ...research.narrative_generators.base import (
            verify_numbers_in_text as _verify, _extract_unit_numbers,
        )
    except Exception:
        # base.py moved/unavailable — лёгкая встроенная проверка
        return _check_numbers_lite(report_md, bundle)

    # Сначала соберём ВСЕ числа из фактов (включая conditions/verbatim)
    fact_nums = _collect_fact_numbers(bundle.facts)
    if not fact_nums:
        return []

    text_nums = _extract_unit_numbers(report_md)
    safe_years = {float(y) for y in range(1990, 2050)}
    halluc = []
    for n in text_nums:
        if n in safe_years:
            continue
        if any(abs(n - fn) < 0.001 for fn in fact_nums):
            continue
        # приближённое (относительная погрешность < 2%)
        if any(fn and abs(n - fn) / abs(fn) < 0.02 for fn in fact_nums if fn):
            continue
        halluc.append(n)
    return halluc[:10]


def _collect_fact_numbers(facts: list[Fact]) -> set[float]:
    """Все числа из фактов (value, conditions, verbatim, qualifications)."""
    try:
        from ...research.narrative_generators.base import (
            _facts_numbers as _collect,
        )
        return _collect(facts)
    except Exception:
        return _collect_fact_numbers_lite(facts)


def _check_numbers_lite(text: str, bundle: KnowledgeBundle) -> list[float]:
    """Лёгкая встроенная проверка если base.py недоступен."""
    fact_nums = _collect_fact_numbers_lite(bundle.facts)
    if not fact_nums:
        return []
    nums = set()
    for m in re.finditer(r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,](\d+))?\s*"
                          r"(?:₽|руб|%|процент|тыс|млн|лет|год|дн|мес)",
                          text, re.IGNORECASE):
        raw = re.sub(r"[ \u00a0\u202f]", "", m.group(1))
        frac = m.group(2)
        try:
            nums.add(float(raw + ("." + frac if frac else "")))
        except ValueError:
            continue
    safe_years = {float(y) for y in range(1990, 2050)}
    halluc = []
    for n in nums:
        if n in safe_years:
            continue
        if any(abs(n - fn) < 0.001 for fn in fact_nums):
            continue
        if any(fn and abs(n - fn) / abs(fn) < 0.02 for fn in fact_nums if fn):
            continue
        halluc.append(n)
    return halluc[:10]


def _collect_fact_numbers_lite(facts: list[Fact]) -> set[float]:
    nums: set[float] = set()
    for f in facts:
        for txt in [f.value, " ".join(f.conditions), f.verbatim]:
            for m in re.finditer(r"\d[\d .,]*", txt or ""):
                raw = re.sub(r"[ .,]", "", m.group(0))
                if raw.isdigit():
                    nums.add(float(raw))
    return nums


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:
        pass
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("{")
    if start < 0:
        return None
    depth = 0; in_str = False; esc = False; end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: end = i + 1; break
    cand = t[start:end] if end > 0 else t[start:].rstrip().rstrip(",") + "}"
    try:
        import json
        return json.loads(cand)
    except Exception:
        return None


def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False
