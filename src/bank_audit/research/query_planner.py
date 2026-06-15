"""Query Planner — генерирует МНОГО разных queries вместо одного.

Проблема: один запрос «{bank} {product}» даёт первую страницу с маркетинговым
текстом. Полная картина рассыпана по: тарифам (PDF), agregators (banki.ru,
sravni.ru), официальным страницам и attribute-specific подстраницам.

Решение: для каждого entity генерируется 8-12 queries разных типов:
  • base       — {bank} {product}
  • tariff     — {bank} {product} тарифы условия pdf
  • site:bank  — site:bank.ru {product}
  • aggregator — site:banki.ru {bank} {product}; site:sravni.ru ...
  • attribute  — {bank} {product} {attribute_label}  ×3 для top-3 core
  • synonym    — {bank} {synonym_of_product}         ×1-2 для известных синонимов

Результат: 10-12 разнообразных URL'ов вместо 3 повторяющихся.

Подход полностью LLM-driven — НЕ хардкод. Получаем core_schema + entity,
LLM сам решает какие attribute names самые важные для targeted queries.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from .entity_extractor import Entity
from .core_schema import CoreAttr

log = logging.getLogger(__name__)


@dataclass
class PlannedQuery:
    """Один запрос с метаданными."""
    text: str               # сам query string
    kind: str               # base/tariff/site_bank/aggregator/attribute/synonym/pdf
    target_attribute: str = ""   # для kind=attribute — имя атрибута
    site_filter: str = ""        # для kind=site_bank/aggregator — домен
    priority: int = 5            # 1-10, выше = важнее


# Известные русские агрегаторы банковской информации
KNOWN_AGGREGATORS = [
    "banki.ru",
    "sravni.ru",
    "bankiros.ru",
]


SYSTEM_PROMPT = """Ты — поисковый аналитик. Твоя задача — на основе банка,
продукта и списка КЛЮЧЕВЫХ атрибутов сгенерировать НАБОР из 4-6 ATTRIBUTE-
SPECIFIC поисковых queries, которые помогут аудитору найти конкретные
значения параметров.

ПРАВИЛА:

1) Каждая query — 3-7 СЛОВ (не предложение, не вопрос).
   ✅ "ВТБ пенсионная карта годовое обслуживание"
   ❌ "Какие условия по пенсионным картам ВТБ?"

2) Каждая query ЦЕЛИТ В КОНКРЕТНЫЙ АТРИБУТ из списка core-схемы.
   Используй понятные пользователю синонимы (а не snake_case).
   ✅ "ВТБ пенсионная карта процент на остаток"     (для процент_на_остаток)
   ✅ "ВТБ пенсионная карта лимит снятия в банкомате" (для дневной_лимит_снятия)
   ❌ "ВТБ пенсионная_карта процент_на_остаток"

3) НЕ ДУБЛИРУЙ queries — каждая должна искать что-то РАЗНОЕ.

4) ВЫБИРАЙ САМЫЕ КРИТИЧНЫЕ для аудитора атрибуты:
   • Деньги: годовое обслуживание, ставка, кешбэк, комиссия
   • Лимиты: снятие, переводы, операции
   • Требования: возраст, документы, доход
   • Регуляторные: гражданство, налогообложение

ВЫХОД: JSON массив 4-6 queries:
[
  {"text": "ВТБ пенсионная карта годовое обслуживание тарифы",
   "target_attribute": "годовое_обслуживание", "priority": 9},
  ...
]

БЕЗ преамбулы, БЕЗ markdown fences."""


async def plan_queries(client: AsyncOpenAI,
                          entity: Entity,
                          core_schema: list[CoreAttr] | None = None,
                          model: str | None = None,
                          n_queries: int = 10) -> list[PlannedQuery]:
    """Генерирует РАЗНОТИПНЫЕ queries для entity (10-12 штук).

    Структура:
      • 1 base query
      • 1-2 tariff/PDF queries
      • 1 site:bank query
      • 2-3 site:aggregator queries
      • 4-6 attribute-specific queries (от LLM)
      • 1-2 synonym queries
    """
    queries: list[PlannedQuery] = []

    bank_name = entity.bank_name
    bank_domain = entity.bank_domain or ""
    product = entity.product
    audience = entity.audience or ""

    # ── 1) Base query (обязательная) ────────────────────────────────────
    queries.append(PlannedQuery(
        text=f"{bank_name} {product}".strip(),
        kind="base", priority=10,
    ))

    # ── 2) Tariff + PDF queries ─────────────────────────────────────────
    queries.append(PlannedQuery(
        text=f"{bank_name} {product} тарифы условия",
        kind="tariff", priority=9,
    ))
    queries.append(PlannedQuery(
        text=f"{bank_name} {product} тарифы pdf",
        kind="pdf", priority=8,
    ))

    # ── 3) site:bank query ──────────────────────────────────────────────
    if bank_domain:
        queries.append(PlannedQuery(
            text=f"site:{bank_domain} {product}",
            kind="site_bank", site_filter=bank_domain, priority=9,
        ))

    # ── 4) Aggregator queries ──────────────────────────────────────────
    for agg in KNOWN_AGGREGATORS[:2]:  # banki.ru + sravni.ru
        queries.append(PlannedQuery(
            text=f"site:{agg} {bank_name} {product}",
            kind="aggregator", site_filter=agg, priority=7,
        ))

    # ── 5) Attribute-specific (LLM-driven) ──────────────────────────────
    # Масштабируем число attribute-queries под размер core-схемы: на 15-20
    # атрибутов 5 запросов не покрывали нишевые комиссии/лимиты (item 48).
    n_attr_q = max(5, min(len(core_schema or []), 12))
    attribute_queries = await _generate_attribute_queries(
        client, entity, core_schema or [], model=model, max_n=n_attr_q,
    )
    queries.extend(attribute_queries)

    # ── 6) Synonym query (если есть) ────────────────────────────────────
    if entity.product_synonyms:
        # Берём один НАИБОЛЕЕ ОТЛИЧАЮЩИЙСЯ от product synonym
        syns = sorted(entity.product_synonyms,
                       key=lambda s: (s.lower() == product.lower(), len(s)))
        for syn in syns[:2]:
            if syn.lower() == product.lower():
                continue
            queries.append(PlannedQuery(
                text=f"{bank_name} {syn}",
                kind="synonym", priority=6,
            ))
            break

    # ── 7) Deduplicate by text ──────────────────────────────────────────
    seen: set[str] = set()
    deduped: list[PlannedQuery] = []
    for q in queries:
        key = q.text.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)

    # ── 8) Sort by priority, limit ──────────────────────────────────────
    deduped.sort(key=lambda x: -x.priority)
    result = deduped[:n_queries]

    log.warning("[query_planner] %s × %s → %s queries (%s)",
                 entity.bank_slug, product[:30], len(result),
                 ", ".join(set(q.kind for q in result)))
    return result


async def _generate_attribute_queries(
    client: AsyncOpenAI, entity: Entity, core_schema: list[CoreAttr],
    model: str | None = None, max_n: int = 5,
) -> list[PlannedQuery]:
    """LLM генерирует attribute-specific queries."""
    if not core_schema:
        return []

    model = model or os.getenv("LLM_MODEL_FAST") or \
              os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")

    attrs_block = "\n".join(
        f"  • {a.name} ({a.label}) — {a.category}, {a.unit or 'без ед.'}"
        for a in core_schema[:15]
    )
    user_msg = (
        f"# Банк: {entity.bank_name}\n"
        f"# Продукт: {entity.product}\n"
        + (f"# Аудитория: {entity.audience}\n" if entity.audience else "")
        + f"\n# Core-атрибуты ({len(core_schema)}):\n{attrs_block}\n\n"
        f"Сгенерируй {max_n} ATTRIBUTE-SPECIFIC search queries для поиска "
        f"конкретных значений КРИТИЧНЫХ для аудитора параметров. "
        f"Верни JSON массив. БЕЗ markdown-fences."
    )

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=800, temperature=0.0,
            ),
            timeout=30,
        )
    except Exception as e:
        log.warning("[query_planner] LLM failed: %s — using fallback", e)
        return _fallback_attribute_queries(entity, core_schema, max_n)

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_array(raw)
    if not isinstance(data, list):
        log.warning("[query_planner] no JSON — fallback")
        return _fallback_attribute_queries(entity, core_schema, max_n)

    out: list[PlannedQuery] = []
    valid_attrs = {a.name for a in core_schema}
    for item in data[:max_n]:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if len(text.split()) < 2 or len(text) > 100:
            continue
        target = (item.get("target_attribute") or "").strip().lower()
        if target and target not in valid_attrs:
            target = ""
        prio = item.get("priority", 7)
        try:
            prio = int(prio)
        except Exception:
            prio = 7
        out.append(PlannedQuery(
            text=text, kind="attribute",
            target_attribute=target, priority=max(1, min(10, prio)),
        ))
    return out


def _fallback_attribute_queries(entity: Entity, core_schema: list[CoreAttr],
                                  max_n: int) -> list[PlannedQuery]:
    """Простой fallback без LLM — берём первые N high-priority core attrs."""
    out = []
    # Берём fee/rate/limit как самые «искабельные»
    priority_cats = {"fee": 9, "rate": 9, "limit": 8, "requirement": 7}
    sorted_attrs = sorted(
        core_schema,
        key=lambda a: -priority_cats.get(a.category, 5),
    )
    for a in sorted_attrs[:max_n]:
        # Конвертируем snake_case в человеческое
        attr_human = a.label or a.name.replace("_", " ")
        out.append(PlannedQuery(
            text=f"{entity.bank_name} {entity.product} {attr_human}",
            kind="attribute",
            target_attribute=a.name,
            priority=priority_cats.get(a.category, 5),
        ))
    return out


def _parse_json_array(raw: str) -> list | None:
    """Извлечь JSON массив с обработкой ```fences```."""
    if not raw:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    cand = t[start:end] if end > 0 else t[start:].rstrip().rstrip(",") + "]"
    try:
        return json.loads(cand)
    except Exception:
        pass
    try:
        return json.loads(re.sub(r",\s*([\]}])", r"\1", cand))
    except Exception:
        return None
