"""EAV-Pipeline Orchestrator — connecting all modules + SSE event streaming.

Заменяет старый stream_deep_analysis. Полный flow:

  1. Entity discovery
  2. Source finding per entity (parallel)
  3. Triple extraction per entity (parallel)
  4. Schema normalization (one call)
  5. Matrix building
  6. Gap-filling for null cells (parallel targeted search)
  7. Render markdown + chart specs

Все стадии стримятся через те же SSE events что и старый pipeline,
поэтому UI работает без изменений.
"""
from __future__ import annotations
import asyncio, json, logging, os, time
from typing import AsyncIterator

from openai import AsyncOpenAI

from ..ai.analyst import LLM_BASE_URL, LLM_API_KEY
from ..ai.llm_utils import (
    _patch_client_reasoning_effort,
    _format_llm_error,
    normalize_question,
)
from .entity_extractor import Entity, extract_entities
from .source_finder import GoldSource, find_gold_sources, find_gold_sources_extended
from .triple_extractor import Triple
from .fact import Fact
from .fact_extractor import extract_facts
from .schema_normalizer import normalize_schema, apply_normalization
from .matrix_builder import Matrix, build_matrix
from .narrative_renderer import (render_narrative_report, extract_chart_specs,
                                   _comparison_table_md)
from .research_brief import synthesize_brief
from .core_schema import CoreAttr, discover_core_schema, build_extract_hint
from .narrative_generators.regulatory_box import REGULATORY_DOMAINS
from .topic_classifier import classify_topic, TopicProfile
from .regulatory_source_finder import find_regulatory_sources
from .llm_throttle import patch_client_throttle
from .gap_filler import fill_gaps

log = logging.getLogger(__name__)


def _trust_marker(score: float) -> int:
    """Trust score → tier 0/1/2 для UI."""
    if score >= 0.9: return 2
    if score >= 0.6: return 1
    return 0


def _verify_facts_against_sources(facts: list, sources_index: list[dict]) -> dict:
    """РЕАЛЬНАЯ (не фейковая) сверка фактов с текстом источников.

    Для каждого числового факта проверяем, что его значение действительно
    присутствует в дословной цитате/выдержке цитируемого источника. Это
    детерминированная проверка (без LLM), даёт ЧЕСТНЫЕ verified/unverified/samples
    вместо прежнего хардкода dropped:0. Текстовые факты (без числа) не сверяем —
    помечаем как unchecked, не раздуваем «verified»."""
    import re as _re
    src_text_by_n: dict[int, str] = {}
    for s in sources_index:
        n = s.get("n")
        if n is None:
            continue
        txt = " ".join(s.get("excerpts") or [])
        src_text_by_n[n] = (txt + " " + (s.get("title") or "")).lower()

    def _nums(t: str) -> set:
        out = set()
        for m in _re.finditer(r"\d[\d  .,]*\d|\d", t or ""):
            raw = _re.sub(r"[  .,]", "", m.group(0))
            if raw.isdigit():
                out.add(raw)
        return out

    verified = 0
    numeric_checked = 0
    unverified_samples: list[dict] = []
    for f in facts:
        if getattr(f, "value_numeric", None) is None:
            continue   # нечисловой факт — не сверяем числом
        numeric_checked += 1
        idx = getattr(f, "source_idx", 0)
        haystack = (src_text_by_n.get(idx, "") + " "
                    + (getattr(f, "verbatim_quote", "") or "")).lower()
        target = _re.sub(r"[  .,]", "", str(getattr(f, "value", "")))
        target_digits = "".join(ch for ch in target if ch.isdigit())
        hit = bool(target_digits) and target_digits in _nums(haystack)
        if hit:
            verified += 1
        elif len(unverified_samples) < 8:
            unverified_samples.append({
                "bank": getattr(f, "entity_bank_slug", ""),
                "attribute": getattr(f, "attribute", ""),
                "value": f"{getattr(f, 'value', '')} {getattr(f, 'unit', '')}".strip(),
                "source_idx": idx,
            })
    return {
        "numeric_checked": numeric_checked,
        "verified": verified,
        "unverified": max(0, numeric_checked - verified),
        "samples": unverified_samples,
    }


def _serialize_matrix(matrix, core_schema) -> dict:
    """Полная матрица в JSON-вид для машиночитаемого экспорта (CSV/JSON) —
    со ВСЕМ контекстом каждой клетки (значение, единица, условия, сегмент,
    исключения, цитата, confidence, ступени). Это и есть «полная картина без
    воды» для самостоятельной сверки аудитором."""
    core_names = [a.name for a in (core_schema or [])]
    insuff = getattr(matrix, "insufficient_banks", set()) or set()
    rows = []
    for attr in matrix.attributes:
        cells = []
        for e in matrix.entities:
            t = matrix.cell(e.bank_slug, attr)
            if t is None:
                cells.append({
                    "bank": e.bank_slug,
                    "state": "no_data" if e.bank_slug in insuff else "not_disclosed",
                    "value": "", "unit": "",
                })
                continue
            members = []
            for m in (getattr(t, "members", None) or []):
                if len(getattr(t, "members", [])) > 1:
                    members.append({
                        "value": m.value, "unit": m.unit,
                        "conditions": list(getattr(m, "conditions", []) or []),
                        "qualifications": getattr(m, "qualifications", "") or "",
                        "source_idx": getattr(m, "source_idx", 0),
                    })
            cells.append({
                "bank": e.bank_slug,
                "state": "ok",
                "value": t.value, "unit": t.unit,
                "value_numeric": t.value_numeric,
                "conditions": list(getattr(t, "conditions", []) or []),
                "qualifications": getattr(t, "qualifications", "") or "",
                "exceptions": list(getattr(t, "exceptions", []) or []),
                "source_idx": t.source_idx,
                "confidence": t.confidence,
                "is_range": getattr(t, "is_range", False),
                "ladder": members,
                "conflict": (e.bank_slug, attr) in (matrix.conflicts or {}),
            })
        rows.append({"attribute": attr, "is_core": attr in core_names, "cells": cells})
    return {
        "banks": [{"slug": e.bank_slug, "name": e.bank_name} for e in matrix.entities],
        "attributes": list(matrix.attributes),
        "core_attributes": core_names,
        "coverage": matrix.coverage,
        "insufficient_banks": sorted(insuff),
        "rows": rows,
    }


def _build_sources_index(entities: list[Entity],
                          sources_per_entity: dict[str, list[GoldSource]]) -> list[dict]:
    """Глобальный список источников с n-индексами. URL дедуп.

    Особый ключ "__regulatory__" — для regulatory sources (общие, не привязаны
    к конкретному банку). Также детектирует kind: bank_official / aggregator /
    regulatory / pdf.
    """
    def _classify_kind(url: str, domain: str, trust: float, is_reg: bool) -> str:
        if is_reg:
            return "regulatory"
        if url.lower().endswith(".pdf"):
            return "pdf"
        if trust >= 0.9:
            return "bank_official"
        return "aggregator"

    index = []
    seen_urls: set[str] = set()
    n = 1

    # Сначала per-entity sources
    for e in entities:
        for s in sources_per_entity.get(e.bank_slug, []):
            if s.url in seen_urls:
                continue
            seen_urls.add(s.url)
            index.append({
                "n": n,
                "url": s.url,
                "title": s.title or s.url[:80],
                "bank_name": e.bank_name,
                "bank_slug": e.bank_slug,
                "domain": s.domain or "",
                "trust_score": s.trust_score,
                "source_kind": _classify_kind(s.url, s.domain or "",
                                                 s.trust_score, False),
                "fetched_at": None,
                "excerpts": [s.text[:600]] if s.text else [],
                "document_id": s.document_id,
                "gold_score": s.gold_score,
            })
            n += 1

    # Затем regulatory sources (не привязаны к bank)
    for s in sources_per_entity.get("__regulatory__", []):
        if s.url in seen_urls:
            continue
        seen_urls.add(s.url)
        index.append({
            "n": n,
            "url": s.url,
            "title": s.title or s.url[:80],
            "bank_name": "",
            "bank_slug": None,
            "domain": s.domain or "",
            "trust_score": s.trust_score,
            "source_kind": _classify_kind(s.url, s.domain or "",
                                             s.trust_score, True),
            "fetched_at": None,
            "excerpts": [s.text[:600]] if s.text else [],
            "document_id": s.document_id,
            "gold_score": s.gold_score,
        })
        n += 1

    return index


def _remap_fact_indices(facts: list[Fact],
                          sources_index: list[dict]) -> list[Fact]:
    """Перенумеровывает source_idx из local в global n из sources_index."""
    url_to_n = {s["url"]: s["n"] for s in sources_index}
    out = []
    for f in facts:
        global_n = url_to_n.get(f.source_url, 0)
        out.append(Fact(
            entity_bank_slug=f.entity_bank_slug,
            attribute=f.attribute,
            value=f.value, unit=f.unit, value_numeric=f.value_numeric,
            conditions=list(f.conditions), qualifications=f.qualifications,
            exceptions=list(f.exceptions), verbatim_quote=f.verbatim_quote,
            page_context=f.page_context, category=f.category,
            audit_priority=f.audit_priority,
            related_attrs=list(f.related_attrs),
            source_idx=global_n,
            source_url=f.source_url,
            confidence=f.confidence,
        ))
    return out


async def stream_eav_research(question: str,
                                history: list[dict] | None = None) -> AsyncIterator[str]:
    """Главный pipeline. Yields SSE-data строки (json-сериализованные events).

    Совместим с frontend форматом SSE (mode/phase/plan/sources/text/done/...).
    """
    started = time.time()
    question = normalize_question(question)

    # ── Setup client ─────────────────────────────────────────────────────
    client = AsyncOpenAI(
        base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
        max_retries=4, timeout=180.0,
    )
    client = _patch_client_reasoning_effort(client)
    # Phase 2: throttle для защиты от 429 rate limits
    client = patch_client_throttle(client, max_concurrent=4)

    # Common helper для SSE
    def evt(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False, default=str)

    yield evt({"type": "mode", "value": "deep"})

    # ── Stage 0: Topic classification (LLM) ──────────────────────────────
    # Определяет: kind темы, нужны ли regulatory источники, какие домены,
    # какие секции отчёта применимы. Управляет стратегиями всех остальных stage.
    yield evt({"type": "phase", "value": "planning"})
    yield evt({"type": "stage_status", "stage": "topic_classification",
                "label": "Классификация темы вопроса",
                "detail": "LLM определяет тип продукта и стратегию поиска",
                "estimate_s": 3})
    try:
        topic_profile = await classify_topic(client, question)
    except Exception as e:
        log.warning("[orchestrator] topic_classify failed: %s", e)
        from .topic_classifier import _default_profile
        topic_profile = _default_profile(question)
    yield evt({"type": "stage_status", "stage": "topic_classified",
                "label": f"Тема: {topic_profile.topic_kind}",
                "detail": topic_profile.summary[:120],
                "estimate_s": 0})

    # ── Stage 1: Entity discovery ────────────────────────────────────────
    yield evt({"type": "stage_status", "stage": "entity_discovery",
                "label": "Извлечение сравниваемых банков и продукта",
                "detail": "LLM анализирует вопрос", "estimate_s": 5})
    try:
        entities = await extract_entities(client, question)
    except Exception as e:
        log.warning("entity_extraction failed: %s", e)
        err = _format_llm_error(e, stage="извлечение entities")
        yield evt({"type": "text", "chunk": err})
        yield evt({"type": "done"})
        return

    if not entities:
        # Никаких entities — общий вопрос. Не можем построить матрицу.
        # Возвращаем понятный fallback.
        yield evt({"type": "text", "chunk":
            "\n⚠ Не удалось извлечь конкретные банки и продукт из вопроса. "
            "Уточните: для каких именно банков и какого продукта нужно сравнение?\n"})
        yield evt({"type": "done"})
        return

    # Plan-event для UI (показывает «шаги» исследования)
    plan_steps = []
    for i, e in enumerate(entities, 1):
        plan_steps.append({
            "n": i,
            "title": f"Сбор источников — {e.bank_name} × {e.product[:40]}",
            "tool": "source_finder",
            "query": f"{e.bank_name} {e.product}",
            "entity": e.bank_slug,
        })
    # Дополнительные шаги для второй и третьей стадий
    next_n = len(plan_steps) + 1
    for e in entities:
        plan_steps.append({
            "n": next_n,
            "title": f"Извлечение фактов — {e.bank_name}",
            "tool": "triple_extractor",
            "query": "", "entity": e.bank_slug,
        })
        next_n += 1
    plan_steps.append({"n": next_n, "title": "Нормализация схемы",
                        "tool": "schema_normalizer", "query": "", "entity": None})
    next_n += 1
    plan_steps.append({"n": next_n, "title": "Заполнение пробелов",
                        "tool": "gap_filler", "query": "", "entity": None})
    yield evt({"type": "plan", "steps": plan_steps})

    log.warning("[orchestrator] %s entities, %s steps", len(entities), len(plan_steps))

    # ── Stage 1.5: Discover CORE SCHEMA — 10-15 ключевых атрибутов.
    # Делаем РАНЬШЕ source_finding, чтобы query_planner мог использовать
    # core_schema для генерации attribute-specific queries.
    yield evt({"type": "stage_status", "stage": "core_schema_discovery",
                "label": "Discovery core-схемы продукта",
                "detail": "LLM выводит 10-15 ключевых атрибутов",
                "estimate_s": 5})
    primary_product = entities[0].product if entities else ""
    primary_audience = entities[0].audience if entities else None
    try:
        core_schema = await discover_core_schema(client, primary_product, primary_audience)
    except Exception as e:
        log.warning("[orchestrator] core_schema discovery failed: %s", e)
        core_schema = []
    extract_hint = build_extract_hint(core_schema) if core_schema else ""
    # Контракт slot_id (этап 3): extractor кладёт факты в закрытый список слотов,
    # matrix джойнит по этому enum → нет рассинхрона имён → нет «пустой таблицы».
    # При SLOT_SCHEMA=1 schema_normalizer не нужен (имена уже = slot_id).
    SLOT_SCHEMA = os.getenv("SLOT_SCHEMA", "1").lower() in ("1", "true", "yes")
    slot_id_set = {a.name for a in core_schema} if (SLOT_SCHEMA and core_schema) else None

    # ── Stage 2: Source discovery (PARALLEL + REGULATORY) ────────────────
    yield evt({"type": "phase", "value": "discovery"})
    yield evt({"type": "stage_status", "stage": "source_finding",
                "label": "Поиск источников (Phase 2: multi-query + PDF + regulatory)",
                "detail": f"Параллельно для {len(entities)} банков + regulatory layer",
                "estimate_s": 30})

    sources_per_entity: dict[str, list[GoldSource]] = {}

    async def find_one_extended(e: Entity, step_idx: int) -> tuple[Entity, list[GoldSource]]:
        """Phase 2: extended finder с query_planner + PDF support."""
        try:
            srcs = await find_gold_sources_extended(
                client, e, core_schema=core_schema, top_n=10,
            )
        except Exception as ex:
            log.warning("[orchestrator] extended_finder failed for %s: %s — "
                          "fallback к base", e.bank_slug, ex)
            try:
                srcs = await find_gold_sources(client, e, top_n=5)
            except Exception as ex2:
                log.warning("base source_finder failed too: %s", ex2)
                srcs = []
        return e, srcs

    # Стартуем step_start события сразу
    for i, e in enumerate(entities, 1):
        yield evt({"type": "step_start", "n": i,
                    "title": f"Сбор источников — {e.bank_name}",
                    "tool": "source_finder_ext", "entity": e.bank_slug})

    # ВЕСЬ STAGE 2 параллельно: per-entity + regulatory
    entity_tasks = [find_one_extended(e, i+1) for i, e in enumerate(entities)]
    regulatory_task = find_regulatory_sources(
        client, question, topic_profile, top_n=4,
    ) if topic_profile.needs_regulatory else asyncio.sleep(0, result=[])

    all_results = await asyncio.gather(
        *entity_tasks, regulatory_task,
        return_exceptions=False,
    )
    entity_results = all_results[:-1]
    regulatory_sources = all_results[-1] if topic_profile.needs_regulatory else []

    for i, (e, srcs) in enumerate(entity_results, 1):
        sources_per_entity[e.bank_slug] = srcs
        yield evt({"type": "step_done", "n": i,
                    "found": len(srcs), "used": len(srcs)})

    # Regulatory sources идут под "shared" slug (None) — общие для всех banks
    if regulatory_sources:
        sources_per_entity["__regulatory__"] = regulatory_sources
        log.warning("[orchestrator] +%s regulatory sources",
                     len(regulatory_sources))

    # Build global sources index
    sources_index = _build_sources_index(entities, sources_per_entity)
    yield evt({"type": "sources", "sources": sources_index})

    total_sources = len(sources_index)
    high = sum(1 for s in sources_index if s.get("trust_score", 0) >= 0.85)
    mid  = sum(1 for s in sources_index if 0.55 <= s.get("trust_score", 0) < 0.85)
    low  = sum(1 for s in sources_index if s.get("trust_score", 0) < 0.55)
    n_pdf = sum(1 for s in sources_index if s.get("url", "").lower().endswith(".pdf"))
    n_reg = len(regulatory_sources)
    # Пер-банковый паритет источников (item 44): считаем источники по банкам и
    # предупреждаем, если у какого-то их СУЩЕСТВЕННО меньше, чем у остальных
    # (сравнение тогда структурно перекошено — отстающим займётся gap_filler).
    src_per_bank = {e.bank_slug: len(sources_per_entity.get(e.bank_slug, []))
                    for e in entities}
    counts = sorted(src_per_bank.values())
    median_src = counts[len(counts) // 2] if counts else 0
    lagging = [next(e.bank_name for e in entities if e.bank_slug == slug)
               for slug, c in src_per_bank.items()
               if c < max(2, 0.5 * median_src)]
    parity_warn = (f"Неравномерный охват источников: меньше всего по "
                   f"{', '.join(lagging)} — сравнение по ним менее полно"
                   if lagging and len(lagging) < len(entities) else None)
    yield evt({"type": "coverage", "total_sources": total_sources,
                "high_trust": high, "mid_trust": mid, "low_trust": low,
                "pdf_sources": n_pdf, "regulatory_sources": n_reg,
                "sources_per_bank": src_per_bank,
                "parity_warning": parity_warn,
                "warning": "Мало источников — отчёт ограничен" if total_sources < 3 else None})

    if total_sources == 0:
        yield evt({"type": "text", "chunk":
            "\n⚠ Не удалось найти источники по теме. Проверьте формулировку вопроса "
            "или попробуйте уточнить продукт / банки.\n"})
        yield evt({"type": "done"})
        return

    # ── Stage 3: Fact extraction (parallel per entity) ───────────────────
    # ОБОГАЩЁННЫЕ Fact'ы заменяют плоские Triple'ы. Каждый факт несёт:
    #   • verbatim_quote — для narrative
    #   • conditions[]   — условия применения
    #   • qualifications — сегмент аудитории
    #   • exceptions[]   — исключения
    #   • category       — fee/rate/limit/feature/requirement/regulation
    #   • audit_priority — high/medium/low
    yield evt({"type": "phase", "value": "fact-extract"})
    yield evt({"type": "stage_status", "stage": "fact_extraction",
                "label": "Извлечение обогащённых фактов из источников",
                "detail": f"Параллельно для {len(entities)} банков",
                "estimate_s": 25})

    fact_step_start_n = len(entities) + 1
    for i, e in enumerate(entities):
        yield evt({"type": "step_start", "n": fact_step_start_n + i,
                    "title": f"Извлечение фактов — {e.bank_name}",
                    "tool": "fact_extractor", "entity": e.bank_slug})

    def _merge_sources(primary: list[GoldSource], extra: list[GoldSource]) -> list[GoldSource]:
        """Объединяет наборы источников по URL (без перезаписи). Восстановленный
        банк должен достигать ТОЙ ЖЕ глубины, что и остальные, а не остаться с
        3 тонкими источниками (item 17)."""
        seen = {s.url for s in primary}
        out = list(primary)
        for s in extra:
            if s.url not in seen:
                seen.add(s.url)
                out.append(s)
        return out

    async def extract_one(e: Entity) -> tuple[Entity, list[Fact]]:
        """Extract с двойной попыткой: если 0 facts — переформулируем product
        на более общий синоним и пробуем повторный source_finder + extract.
        Второй заход идёт на ПОЛНОЙ глубине (top_n≈8) и МЁРДЖИТ источники."""
        srcs = sources_per_entity.get(e.bank_slug, [])
        facts: list[Fact] = []
        if srcs:
            try:
                facts = await extract_facts(client, e, srcs,
                                              core_schema_hint=extract_hint,
                                              slot_ids=slot_id_set)
            except Exception as ex:
                log.warning("extract_facts failed for %s: %s", e.bank_slug, ex)

        # SECOND CHANCE при 0 facts — полная глубина + мёрдж источников
        if not facts and e.product_synonyms:
            general_syn = sorted(e.product_synonyms,
                                  key=lambda s: (s.lower() == e.product.lower(), len(s)))
            for alt_product in general_syn[:2]:
                if alt_product.lower() == e.product.lower():
                    continue
                alt_e = Entity(
                    bank_slug=e.bank_slug, bank_name=e.bank_name,
                    bank_domain=e.bank_domain, product=alt_product,
                    product_synonyms=e.product_synonyms,
                    audience=e.audience,
                )
                log.warning("[orchestrator] %s: 0 facts → retry product=%r (top_n=8)",
                             e.bank_slug, alt_product)
                try:
                    try:
                        alt_srcs = await find_gold_sources_extended(
                            client, alt_e, core_schema=core_schema, top_n=8)
                    except Exception:
                        alt_srcs = await find_gold_sources(client, alt_e, top_n=8)
                    if alt_srcs:
                        merged = _merge_sources(srcs, alt_srcs)
                        sources_per_entity[e.bank_slug] = merged
                        srcs = merged
                        facts = await extract_facts(client, alt_e, merged,
                                                       core_schema_hint=extract_hint,
                                                       slot_ids=slot_id_set)
                        if facts:
                            for f in facts:
                                f.entity_bank_slug = e.bank_slug
                            break
                except Exception as ex:
                    log.info("retry failed for %s with %r: %s",
                              e.bank_slug, alt_product, ex)

        # БОЛЬШЕ НЕ инжектим placeholder-факт «продукт_доступен» в данные.
        # 0 фактов → банк помечается insufficient (см. ниже), его пустые клетки
        # рендерятся как «нет данных — источник не прочитан», а НЕ как факт.
        return e, facts

    fact_results = await asyncio.gather(
        *[extract_one(e) for e in entities], return_exceptions=False)
    all_facts: list[Fact] = []
    insufficient_banks: set[str] = set()
    for i, (e, facts) in enumerate(fact_results):
        all_facts.extend(facts)
        if not facts:
            insufficient_banks.add(e.bank_slug)
        n_high = sum(1 for f in facts if f.audit_priority == "high")
        yield evt({"type": "step_done", "n": fact_step_start_n + i,
                    "found": len(facts), "used": len(facts),
                    "detail": f"{n_high} priority-high" if facts else "нет данных (источник не прочитан)"})

    # ── Stage 3.5: Extract из regulatory sources (если есть) ─────────────
    # Создаём synthetic entity «Регулятор» для extract из НПА-документов.
    # Эти факты используются regulatory_box секцией.
    if regulatory_sources:
        try:
            reg_entity = Entity(
                bank_slug="_regulator",
                bank_name="Регулятор / НПА",
                bank_domain="",
                product=question[:100],
                audience=None,
                product_synonyms=[],
            )
            reg_extract_hint = (extract_hint or "") + (
                "\n# ВАЖНО: это РЕГУЛЯТОРНЫЕ источники (ГК РФ / ЦБ / нотариат / "
                "Минобороны и т.п.). Извлекай НОРМАТИВНЫЕ факты:\n"
                "  • статьи закона, конкретные пункты, сроки\n"
                "  • obligations / запреты / разрешения\n"
                "  • категорию ставь REGULATION для каждого извлечённого факта\n"
            )
            reg_facts = await extract_facts(
                client, reg_entity, regulatory_sources,
                core_schema_hint=reg_extract_hint,
            )
            # Принудительно — категория regulation
            for f in reg_facts:
                if f.attribute != "продукт_доступен":
                    f.category = "regulation"
                    if f.audit_priority == "low":
                        f.audit_priority = "medium"
            # Skip placeholder
            reg_facts = [f for f in reg_facts if f.attribute != "продукт_доступен"]
            if reg_facts:
                all_facts.extend(reg_facts)
                log.warning("[orchestrator] +%s regulatory facts extracted",
                             len(reg_facts))
        except Exception as e:
            log.warning("[orchestrator] regulatory extract failed: %s", e)

    # ── Stage 4: Schema normalization ────────────────────────────────────
    # При SLOT_SCHEMA (контракт slot_id) имена фактов УЖЕ канонические (= slot_id),
    # отдельный LLM-вызов-нормализатор не нужен — это была точка отказа «пустой
    # таблицы». Нормализуем только в legacy-режиме (SLOT_SCHEMA=0) или если
    # core-схема пуста (тогда имена свободные).
    mapping: dict = {}
    if slot_id_set:
        normalized_facts = all_facts   # имена = slot_id, нормализация не требуется
        log.warning("[orchestrator] SLOT_SCHEMA on → schema_normalizer пропущен "
                     "(имена фактов = slot_id)")
    else:
        yield evt({"type": "stage_status", "stage": "schema_normalization",
                    "label": "Сведение синонимичных полей",
                    "detail": f"Из {len(all_facts)} фактов", "estimate_s": 8})
        yield evt({"type": "step_start", "n": fact_step_start_n + len(entities),
                    "title": "Нормализация схемы атрибутов",
                    "tool": "schema_normalizer", "entity": None})
        core_attr_names_pre = [a.name for a in core_schema]
        try:
            mapping = await normalize_schema(client, all_facts,
                                              core_attrs=core_attr_names_pre)
            normalized_facts = apply_normalization(all_facts, mapping)
        except Exception as e:
            log.warning("schema_normalize failed: %s", e)
            normalized_facts = all_facts

    # АВАРИЙНЫЙ core: если LLM-discovery вернул пусто, выводим core-схему из
    # самих фактов (иначе таблица и знаменатель покрытия пустые).
    if not core_schema and normalized_facts:
        try:
            from .core_schema import derive_core_from_facts
            core_schema = derive_core_from_facts(normalized_facts)
            extract_hint = build_extract_hint(core_schema) if core_schema else extract_hint
            log.warning("[orchestrator] core_schema пуст → derive из фактов: %s attrs",
                         len(core_schema))
        except Exception as e:
            log.warning("[orchestrator] derive_core_from_facts failed: %s", e)

    yield evt({"type": "step_done", "n": fact_step_start_n + len(entities),
                "found": len(set(f.attribute for f in normalized_facts)),
                "used": len(normalized_facts)})

    # Re-map source_idx to global n
    normalized_facts = _remap_fact_indices(normalized_facts, sources_index)

    # ── Stage 5: Matrix build ────────────────────────────────────────────
    core_attr_names = [a.name for a in core_schema]
    matrix = build_matrix(entities, normalized_facts, sources_index,
                           core_attrs=core_attr_names,
                           insufficient_banks=insufficient_banks)
    initial_coverage = matrix.coverage

    # ── Stage 5.5: Gap-filling — ПО-БАНКОВЫЙ триггер, не глобальный 0.85 ──
    # Раньше gap_filler запускался только при средней coverage<0.85, поэтому
    # «95% по А + 20% по Б» (среднее >0.85) пропускал добор и оставлял банк Б
    # дырявым. Теперь триггерим, если ЛЮБОЙ банк ниже порога ИЛИ заметно ниже
    # лидера (асимметрия) ИЛИ есть пустые high-priority core-клетки.
    def _needs_gap_fill(m) -> bool:
        if not core_schema:
            return False
        per_bank = {e.bank_slug: m.bank_coverage(e.bank_slug, core_attr_names)
                    for e in entities}
        if not per_bank:
            return m.coverage < 0.85
        best = max(per_bank.values())
        for slug, cov in per_bank.items():
            if cov < 0.7:               # абсолютный пол по банку
                return True
            if best - cov > 0.25:        # асимметрия относительно лидера
                return True
        return m.coverage < 0.85

    if _needs_gap_fill(matrix):
        yield evt({"type": "stage_status", "stage": "gap_filling",
                    "label": "Заполнение пробелов (targeted web search)",
                    "detail": f"Coverage {round(matrix.coverage * 100)}% / "
                                "выравниваем отстающие банки",
                    "estimate_s": 30})
        try:
            gap_facts, _ = await fill_gaps(
                client, matrix, entities, core_schema, sources_index,
                build_matrix_fn=lambda e, f, s, c: build_matrix(
                    e, f, s, core_attrs=c, insufficient_banks=insufficient_banks),
                initial_facts=normalized_facts,
            )
        except Exception as e:
            log.warning("[orchestrator] gap_filler failed: %s", e)
            gap_facts = []

        if gap_facts:
            # Нормализация новых фактов через тот же mapping
            try:
                gap_facts = apply_normalization(gap_facts, mapping)
            except Exception as e:
                log.warning("apply_normalization for gap_facts failed: %s", e)
            normalized_facts = normalized_facts + gap_facts
            # банк, по которому добор дал факты, больше не «insufficient»
            insufficient_banks -= {getattr(f, "entity_bank_slug", "") for f in gap_facts}
            # Re-build матрицы
            matrix = build_matrix(entities, normalized_facts, sources_index,
                                    core_attrs=core_attr_names,
                                    insufficient_banks=insufficient_banks)
            log.warning("[orchestrator] after gap_filler: %s facts (+%s), "
                          "coverage %.0f%% (+%.0f%%)",
                          len(normalized_facts), len(gap_facts),
                          matrix.coverage * 100,
                          (matrix.coverage - initial_coverage) * 100)

    # ── РЕАЛЬНАЯ верификация (не театр): сверяем числа фактов с источниками ──
    vres = _verify_facts_against_sources(normalized_facts, sources_index)
    yield evt({"type": "verification",
                "method": "numeric_grounding",   # честный ярлык метода
                "numeric_checked": vres["numeric_checked"],
                "verified": vres["verified"],
                "unverified": vres["unverified"],
                "valid_citations": sorted({f.source_idx for f in normalized_facts if f.source_idx}),
                "checked": vres["numeric_checked"] > 0})
    yield evt({"type": "claim_check",
                "verified": vres["verified"],
                "dropped": vres["unverified"],
                "samples": vres["samples"]})

    # ── Stage 5.7: РАННЯЯ ОТДАЧА таблицы (perceived latency) ─────────────
    # Самый ценный артефакт — сравнительная таблица + графики — готов сразу после
    # матрицы (детерминированно, без LLM). Отдаём его ДО brief/outline/секций/
    # критика (~60-70с раньше) — пользователь видит «мясо», нарратив идёт следом.
    preview_emitted = False
    early_charts = []
    try:
        n_banks = len(entities)
        n_attrs = len(matrix.attributes)
        cov_pct = round(matrix.coverage * 100)
        n_facts = len(normalized_facts)
        n_high = sum(1 for f in normalized_facts if f.audit_priority == "high")
        yield evt({"type": "text", "chunk": f"# Аудит-отчёт: {question}\n\n"})
        yield evt({"type": "text", "chunk": (
            f"_Сравнение **{n_banks} банков** по **{n_attrs}** параметрам — "
            f"всего **{n_facts}** фактов извлечено ({n_high} приоритет high), "
            f"покрытие core-схемы **{cov_pct}%**._\n\n")})
        yield evt({"type": "text", "chunk": _comparison_table_md(matrix) + "\n\n"})
        # Полная матрица (машиночитаемо) — для кнопки «Скачать CSV/JSON».
        yield evt({"type": "matrix", "data": _serialize_matrix(matrix, core_schema)})
        early_charts = extract_chart_specs(matrix)
        for ch in early_charts:
            yield evt({"type": "chart", "spec": ch})
            await asyncio.sleep(0.05)
        preview_emitted = True
        yield evt({"type": "stage_status", "stage": "preview_ready",
                    "label": "Сравнительная таблица готова",
                    "detail": "Выводы и нарратив формируются…", "estimate_s": 0})
    except Exception as e:
        log.warning("[orchestrator] ранняя отдача таблицы не удалась: %s", e)

    # Структурный список пробелов (item 41) — first-class сигнал для UI/алертов.
    try:
        gap_items = []
        for a in core_attr_names:
            miss = [e.bank_name for e in entities if matrix.cell(e.bank_slug, a) is None]
            if miss:
                gap_items.append({"attribute": a, "missing_banks": miss,
                                   "all": len(miss) == len(entities)})
        yield evt({"type": "gaps",
                    "insufficient_banks": sorted(insufficient_banks),
                    "missing": gap_items})
    except Exception as e:
        log.warning("[orchestrator] gaps event failed: %s", e)

    # ── Stage 6: Narrative outline planning + section generation ─────────
    yield evt({"type": "phase", "value": "synthesizing"})
    yield evt({"type": "stage_status", "stage": "outline_planning",
                "label": "Планирование структуры отчёта (LLM)",
                "detail": "Выбор 5-8 секций оптимальных для темы",
                "estimate_s": 5})

    # Detect regulatory sources (учитывая обе сигнатуры: domains и kind)
    has_reg = (topic_profile.needs_regulatory or
                any(s.get("source_kind") == "regulatory" or
                     s.get("domain", "") in REGULATORY_DOMAINS
                     for s in sources_index))

    # ── Stage 6.0: ГЛОБАЛЬНЫЙ СИНТЕЗ (research_brief) ────────────────────
    # При SYNTH_UNIFIED единый синтез-генератор САМ делает аналитику (он и есть
    # «мозг»), поэтому отдельный research_brief избыточен — пропускаем его, чтобы
    # не делать второй тяжёлый reasoning-вызов (он же чаще всего и упирался в
    # таймаут). В legacy-режиме brief по-прежнему питает 9 генераторов.
    _SYNTH_UNIFIED = os.getenv("SYNTH_UNIFIED", "1").lower() in ("1", "true", "yes")
    brief = None
    if not _SYNTH_UNIFIED:
        yield evt({"type": "stage_status", "stage": "synthesis_brief",
                    "label": "Глобальный аналитический синтез",
                    "detail": "Единый разбор всей картины перед секциями",
                    "estimate_s": 25})
        try:
            brief = await synthesize_brief(
                client, question, entities, normalized_facts, matrix,
                sources_index, core_schema=core_schema)
        except Exception as e:
            log.warning("[orchestrator] research_brief failed: %s", e)
    if brief:
        yield evt({"type": "stage_status", "stage": "synthesis_brief_done",
                    "label": "Синтез готов",
                    "detail": f"{len(brief.insights)} инсайтов, тезис сформирован",
                    "estimate_s": 0})

    # Narrative-генерация
    yield evt({"type": "stage_status", "stage": "narrative_render",
                "label": "Генерация narrative-секций (под меморандум)",
                "detail": "key_findings, per_bank, pricing, risks…",
                "estimate_s": 30})

    # NB: НЕ фильтруем факты до brief/секций (низкоприоритетные «фичи» иногда
    # объясняют механику продукта). Релевантность даёт section-aware отбор внутри
    # генераторов (select_facts_for_section), а не предварительная отсечка.
    try:
        used_sections, report_md = await render_narrative_report(
            client=client,
            model=os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                              "gpt-4o-mini"),
            question=question,
            entities=entities,
            facts=normalized_facts,
            matrix=matrix,
            sources_index=sources_index,
            core_schema=core_schema,
            has_regulatory=has_reg,
            topic_profile=topic_profile,
            brief=brief,
            preview_emitted=preview_emitted,
        )
    except Exception as e:
        log.exception("[orchestrator] narrative_render failed: %s", e)
        used_sections, report_md = [], (
            f"# Аудит-отчёт: {question}\n\n"
            f"⚠ Не удалось сгенерировать narrative-отчёт. "
            f"Найдено {len(normalized_facts)} фактов по {len(entities)} банкам, "
            f"покрытие core-схемы {round(matrix.coverage * 100)}%."
        )

    # Outline event для UI (теперь dynamic, не hardcoded)
    yield evt({"type": "outline", "sections": [s.title for s in used_sections]})

    # Stream markdown chunk-by-chunk (для красивого UI-отображения)
    paragraphs = report_md.split("\n\n")
    for p in paragraphs:
        if not p.strip():
            continue
        yield evt({"type": "text", "chunk": p + "\n\n"})
        await asyncio.sleep(0.03)

    # ── Stage 7: Charts from matrix ──────────────────────────────────────
    # Если графики уже отданы в раннем preview — не дублируем.
    if preview_emitted:
        charts = early_charts
    else:
        yield evt({"type": "phase", "value": "charting"})
        charts = extract_chart_specs(matrix)
        for ch in charts:
            yield evt({"type": "chart", "spec": ch})
            await asyncio.sleep(0.1)

    # ── Done ────────────────────────────────────────────────────────────
    elapsed = time.time() - started
    n_high = sum(1 for f in normalized_facts if f.audit_priority == "high")
    log.warning("[orchestrator] DONE in %.1fs: %s entities, %s sources, "
                 "%s facts (%s high), %s sections, %s charts, coverage=%.0f%%",
                 elapsed, len(entities), total_sources, len(normalized_facts),
                 n_high, len(used_sections), len(charts), matrix.coverage * 100)
    yield evt({"type": "phase", "value": "done"})
    yield evt({"type": "done"})
