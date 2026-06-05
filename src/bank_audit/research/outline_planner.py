"""Outline Planner — LLM решает какие 5-8 секций нужны для отчёта.

В отличие от старого «один шаблон на все случаи», для каждой темы выбирается
оптимальный набор:
  • доверенности → +regulatory_box (ГК РФ), +cant_do_box
  • ипотека → +government_programs_box, +requirements_breakdown
  • эквайринг → +pricing_breakdown с пакетами, +integration_box
  • вклады → +deposit_terms_box, +regulatory_box (страхование АСВ)

Структура секций определяется на основе:
  • вопроса
  • core_schema (категории атрибутов)
  • примеров фактов (что реально нашлось)
  • категорий fact'ов (есть ли regulation/feature/fee/...)
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass
from collections import Counter

from openai import AsyncOpenAI

from .fact import Fact
from .core_schema import CoreAttr

log = logging.getLogger(__name__)


# Допустимые типы секций отчёта (со spec'ом контента для каждого)
SECTION_KINDS = {
    "key_findings":           "3-5 главных инсайтов аудитора (narrative)",
    "comparison_table":       "Таблица банки × core атрибуты",
    "per_entity_breakdown":   "Per-bank narrative секция с цитатами",
    "pricing_breakdown":      "Детальная разбивка стоимости / тарифов",
    "digital_channels":       "Сравнение дистанционных сервисов",
    "regulatory_box":         "Цитаты НПА и регулятора (ГК/НК/ЦБ/ФНП)",
    "cant_do_box":            "Что НЕЛЬЗЯ делать (negative list)",
    "requirements_box":       "Требования к клиенту (документы, возраст, доход)",
    "government_programs":    "Программы господдержки (для соц. продуктов)",
    "conflicts_explained":    "Расхождения в источниках с интерпретацией",
    "risks_recommendations":  "Риски + actionable рекомендации аудитору",
}


SYSTEM_PROMPT = """Ты — структурный планировщик аудит-отчётов. На основе
вопроса аудитора, списка core-атрибутов и образцов фактов выбираешь
ОПТИМАЛЬНЫЙ набор из 5-8 секций отчёта.

Допустимые типы секций (выбирай из них):
""" + "\n".join(f"  • {k} — {v}" for k, v in SECTION_KINDS.items()) + """

ПРАВИЛА:

1) ВСЕГДА включай:
   • key_findings — обязательно первой
   • comparison_table — обязательно второй
   • per_entity_breakdown — обязательно
   • risks_recommendations — обязательно последней

2) ВКЛЮЧАЙ КОНДИЦИОНАЛЬНО:
   • pricing_breakdown — если есть много fee/rate атрибутов и они РАЗНЯТСЯ
   • digital_channels — если в фактах есть feature про онлайн/приложение
   • regulatory_box — если тема юридическая (доверенность, страхование вкладов,
                       военная ипотека, маткапитал) или в фактах есть regulation
   • cant_do_box — если в фактах явно есть ограничения/запреты (доверенность,
                    переводы, операции по доверенности)
   • requirements_box — если много requirement-фактов (документы, возраст, доход)
   • government_programs — для соц. продуктов (ипотека, ветераны, пенсионеры)
   • conflicts_explained — только если в фактах указано наличие конфликтов

3) ПОРЯДОК: key_findings → table → детальные секции → spec. боксы → risks/recs

4) Каждой секции дай ЦЕЛЬ и ФОКУС (1-2 предложения):
   {"kind": "regulatory_box",
    "title": "Регуляторное поле",
    "focus": "Цитаты ГК РФ ст.185-189 и информ-письма ЦБ от 2024 года",
    "audit_relevance": "Аудитору важно знать какие документы обязательны"}

5) Стандартный набор для PRODUCT-сравнения (5 банков по карте):
   [key_findings, comparison_table, per_entity_breakdown,
    pricing_breakdown, digital_channels, risks_recommendations]
   Можно добавить regulatory_box если есть НПА в фактах.

ВЫХОД: JSON массив секций. БЕЗ преамбулы, БЕЗ markdown-fences.

Каждый element:
{
  "kind": "<one of SECTION_KINDS>",
  "title": "Заголовок секции в отчёте (на русском)",
  "focus": "Что именно должно быть в этой секции (1-2 предложения)",
  "audit_relevance": "Почему это важно аудитору (1 предложение)"
}"""


@dataclass
class Section:
    kind: str
    title: str
    focus: str
    audit_relevance: str = ""


def _parse_array(raw: str) -> list | None:
    if not raw:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("[")
    if start < 0: return None
    depth = 0; in_str = False; esc = False; end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0: end = i + 1; break
    cand = t[start:end] if end > 0 else (t[start:].rstrip().rstrip(",") + "]")
    try: return json.loads(cand)
    except Exception: pass
    try: return json.loads(re.sub(r",\s*([\]}])", r"\1", cand))
    except Exception: return None


def _facts_summary(facts: list[Fact], limit: int = 10) -> str:
    """Сжатый ёр фактов для prompt'а (топ-N разных attrs)."""
    seen_attrs: dict[str, Fact] = {}
    for f in facts:
        if f.attribute not in seen_attrs:
            seen_attrs[f.attribute] = f
    samples = list(seen_attrs.values())[:limit]
    lines = []
    for f in samples:
        lines.append(f"  • [{f.entity_bank_slug}] {f.attribute}={f.value} {f.unit} "
                     f"({f.category}/{f.audit_priority})")
    return "\n".join(lines)


def _category_stats(facts: list[Fact]) -> dict[str, int]:
    return dict(Counter(f.category for f in facts))


async def plan_sections(client: AsyncOpenAI, question: str,
                          core_schema: list[CoreAttr],
                          facts: list[Fact],
                          has_regulatory_sources: bool = False,
                          suggested_kinds: list[str] | None = None,
                          model: str | None = None) -> list[Section]:
    """Возвращает список 5-8 секций для отчёта.

    suggested_kinds — подсказка от topic_classifier (какие секции скорее
    подходят теме); LLM имеет полное право не использовать или дополнить.
    """
    model = model or os.getenv("LLM_MODEL_FAST") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini")
    cat_stats = _category_stats(facts)
    core_attrs_list = ", ".join(a.name for a in core_schema[:15])
    facts_sample = _facts_summary(facts, 12)
    suggested_block = ""
    if suggested_kinds:
        suggested_block = (f"# Topic-classifier рекомендует секции (можешь следовать или дополнить):\n"
                            f"  {', '.join(suggested_kinds)}\n\n")
    user_msg = (
        f"# Вопрос аудитора\n{question}\n\n"
        f"# Core-атрибуты темы ({len(core_schema)}):\n  {core_attrs_list}\n\n"
        f"# Категории фактов: {cat_stats}\n"
        f"# Регуляторные источники подгружены: {'да' if has_regulatory_sources else 'нет'}\n\n"
        + suggested_block +
        f"# Sample фактов:\n{facts_sample}\n\n"
        f"Выбери 5-8 секций для аудит-отчёта. Верни JSON массив."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=2000, temperature=0.0,
            ), timeout=30,
        )
    except Exception as e:
        log.warning("[outline_planner] LLM failed: %s — fallback на default", e)
        return _default_outline(facts)

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_array(raw)
    if not isinstance(data, list) or not data:
        log.warning("[outline_planner] no JSON array (raw 200=%r) — fallback", raw[:200])
        return _default_outline(facts)

    sections: list[Section] = []
    seen_kinds: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = (item.get("kind") or "").strip().lower()
        if kind not in SECTION_KINDS or kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        sections.append(Section(
            kind=kind,
            title=(item.get("title") or kind.replace("_", " ").capitalize()).strip(),
            focus=(item.get("focus") or "").strip(),
            audit_relevance=(item.get("audit_relevance") or "").strip(),
        ))

    # Гарантируем обязательные секции
    must_have = ["key_findings", "comparison_table", "per_entity_breakdown",
                 "risks_recommendations"]
    for mh in must_have:
        if mh not in seen_kinds:
            sections.append(Section(
                kind=mh,
                title=SECTION_KINDS[mh].split("—")[0].strip().capitalize(),
                focus=SECTION_KINDS[mh],
                audit_relevance="",
            ))

    # Сортировка: обязательные первыми/последними, остальные в исходном порядке
    ordered: list[Section] = []
    # 1. key_findings
    for s in sections:
        if s.kind == "key_findings": ordered.append(s); break
    # 2. comparison_table
    for s in sections:
        if s.kind == "comparison_table": ordered.append(s); break
    # 3. per_entity_breakdown
    for s in sections:
        if s.kind == "per_entity_breakdown": ordered.append(s); break
    # 4. опциональные в порядке появления
    placed = {s.kind for s in ordered}
    for s in sections:
        if s.kind not in placed and s.kind != "risks_recommendations":
            ordered.append(s); placed.add(s.kind)
    # 5. risks_recommendations всегда последней
    for s in sections:
        if s.kind == "risks_recommendations": ordered.append(s); break

    log.warning("[outline_planner] %s sections: %s",
                len(ordered), [s.kind for s in ordered])
    return ordered


def _default_outline(facts: list[Fact]) -> list[Section]:
    """Fallback если LLM упал."""
    return [
        Section("key_findings",         "Ключевые выводы", "Топ-инсайтов сравнения"),
        Section("comparison_table",     "Сравнительная таблица", "Core атрибуты × банки"),
        Section("per_entity_breakdown", "Детально по каждому банку", "Per-bank разбор"),
        Section("risks_recommendations","Риски и рекомендации", "Что проверить аудитору"),
    ]
