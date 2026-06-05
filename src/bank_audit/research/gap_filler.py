"""Gap Filler — итеративное заполнение пустых клеток матрицы.

После первого прохода extraction coverage обычно 30-50%: half-empty matrix.
Demo-качество требует 70-85%. Эту разницу закрывает gap_filler:

  1. Анализирует matrix.null_cells() → какие (bank, attribute) пустые
  2. Группирует: если у ВСЕХ банков attr пуст → skip (нет публичной инфы)
  3. Для приоритетных пробелов генерирует TARGETED queries
     («ВТБ пенсионная карта годовое обслуживание тариф»)
  4. Mini-search (1-2 URLs per query), focused extract
  5. Merge новые facts в общий список → пересборка матрицы

Бюджет:
  • Max 2 итерации
  • Max 10 targeted queries за всю стадию
  • Stop если coverage растёт меньше чем на 5%

Stability:
  • Если LLM или search падает — стадия НЕ блокирует pipeline
    (просто меньше фактов, но рендер всё равно произойдёт)
  • Все вызовы за пределы LLM/HTTP — graceful с timeout
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
from typing import Awaitable, Callable

from openai import AsyncOpenAI

from .entity_extractor import Entity
from .fact import Fact
from .fact_extractor import extract_facts
from .source_finder import GoldSource
from .matrix_builder import Matrix
from .core_schema import CoreAttr
from ..rag.web_search import search as web_search
from ..rag.fetcher import fetch as fetch_url
from .pdf_extractor import extract_pdf_text, is_pdf_url

log = logging.getLogger(__name__)


# Бюджет и пороги
MAX_ITERATIONS         = 2
MAX_QUERIES_TOTAL      = 10
MAX_QUERIES_PER_ITER   = 6
MIN_COVERAGE_GAIN      = 0.05    # 5% — иначе stop
MAX_URLS_PER_QUERY     = 2
MAX_PARALLEL_FETCH     = 4


def _humanize_attribute(attr_name: str, core_schema: list[CoreAttr]) -> str:
    """snake_case → человеческая фраза для query."""
    for a in core_schema:
        if a.name == attr_name and a.label:
            return a.label
    return attr_name.replace("_", " ")


def _prioritize_gaps(matrix: Matrix, core_schema: list[CoreAttr],
                       max_n: int = 6) -> list[tuple[str, str]]:
    """Возвращает приоритетные (bank_slug, attribute) для дозаполнения.

    Логика приоритизации:
      • core attrs > non-core
      • категория fee/rate/limit > feature
      • атрибуты с partial-coverage (есть данные у >=1 банка) > вообще пустые
    """
    nulls = matrix.null_cells()
    if not nulls:
        return []

    core_names = {a.name for a in core_schema}
    core_priority = {
        "fee": 9, "rate": 9, "limit": 8,
        "requirement": 7, "regulation": 7,
        "feature": 5,
    }
    attr_category = {a.name: a.category for a in core_schema}

    # Считаем coverage per attribute (по матрице)
    attr_filled = {}
    for attr in matrix.attributes:
        attr_filled[attr] = sum(1 for e in matrix.entities
                                   if matrix.cell(e.bank_slug, attr) is not None)
    n_banks = len(matrix.entities)

    def _score(gap: tuple[str, str]) -> float:
        bank, attr = gap
        s = 0.0
        if attr in core_names:
            s += 10.0
        s += core_priority.get(attr_category.get(attr, "feature"), 5)
        # Partial coverage даёт boost (другие банки заполнены — есть надежда дозаполнить)
        filled = attr_filled.get(attr, 0)
        if filled > 0 and filled < n_banks:
            s += 5.0   # «у других есть, у этого нет» — приоритет
        elif filled == 0:
            s -= 3.0   # «у всех нет» — низкий приоритет (нет публичной инфы)
        return s

    nulls_sorted = sorted(nulls, key=lambda g: -_score(g))
    return nulls_sorted[:max_n]


async def _targeted_search_and_extract(client: AsyncOpenAI,
                                          entity: Entity,
                                          gap_attr_label: str,
                                          target_attr: str,
                                          core_schema: list[CoreAttr],
                                          model: str | None = None) -> list[Fact]:
    """1 targeted gap: query → 2 URLs → fetch → extract focused.
    Возвращает 0-N новых фактов (может найти не только target_attr но и соседние)."""

    query = f"{entity.bank_name} {entity.product} {gap_attr_label}"

    # Search
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(
            None,
            lambda: web_search(query, max_results=MAX_URLS_PER_QUERY) or [],
        )
    except Exception as e:
        log.info("[gap_filler] search %r failed: %s", query, e)
        return []
    urls = [r.get("url", "") for r in results if r.get("url")][:MAX_URLS_PER_QUERY]
    if not urls:
        return []

    # Fetch text для URLs (HTML или PDF)
    sources: list[GoldSource] = []
    sem = asyncio.Semaphore(MAX_PARALLEL_FETCH)

    async def _one_url(url: str) -> GoldSource | None:
        async with sem:
            if is_pdf_url(url):
                try:
                    text = await extract_pdf_text(url)
                except Exception:
                    text = ""
            else:
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, fetch_url, url),
                        timeout=15,
                    )
                except Exception:
                    return None
                if not result or not getattr(result, "content", None) or result.status != 200:
                    return None
                try:
                    html = result.content.decode("utf-8", errors="replace")
                except Exception:
                    return None
                text = _html_to_text(html)
            if not text or len(text) < 300:
                return None
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower().removeprefix("www.")
            trust = 0.95 if entity.bank_domain and entity.bank_domain in host else 0.55
            return GoldSource(
                url=url, title=url.split("/")[-1][:120],
                bank_slug=entity.bank_slug, domain=host,
                trust_score=trust, text=text[:6000],
                length=len(text), gold_score=trust * 0.8,
                document_id=None,
            )

    fetched = await asyncio.gather(*[_one_url(u) for u in urls],
                                       return_exceptions=False)
    sources = [s for s in fetched if s]
    if not sources:
        return []

    # Focused extract: подсказка LLM что искать конкретно этот атрибут
    extract_hint = (f"\n# ВАЖНО: целевой атрибут — '{target_attr}' "
                     f"({gap_attr_label}). Извлеки в первую очередь его значение, "
                     f"но также все связанные факты этого продукта.\n")
    try:
        new_facts = await extract_facts(client, entity, sources,
                                          core_schema_hint=extract_hint,
                                          model=model)
    except Exception as e:
        log.info("[gap_filler] extract for %s/%s failed: %s",
                  entity.bank_slug, target_attr, e)
        return []

    # Если pipeline вернул placeholder «продукт_доступен» — это значит реально
    # ничего нового не найдено. Skip.
    if (len(new_facts) == 1 and
          new_facts[0].attribute == "продукт_доступен"):
        return []

    log.warning("[gap_filler] %s × %s → %s new facts",
                 entity.bank_slug, target_attr, len(new_facts))
    return new_facts


def _html_to_text(html: str) -> str:
    """Упрощённый HTML → text."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html,
                    flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text,
                    flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def fill_gaps(client: AsyncOpenAI,
                      matrix: Matrix,
                      entities: list[Entity],
                      core_schema: list[CoreAttr],
                      sources_index: list[dict],
                      build_matrix_fn: Callable,
                      initial_facts: list[Fact] | None = None,
                      model: str | None = None) -> tuple[list[Fact], Matrix]:
    """Итеративное заполнение пробелов.

    Возвращает (new_facts, updated_matrix).

    initial_facts — все ранее собранные facts. Нужны чтобы temp-matrix
    внутри loop'а правильно считал coverage (старые + новые). Без них
    temp matrix содержит только новые facts и coverage искажается.

    build_matrix_fn — функция (entities, facts, sources_index, core_attrs) → Matrix
    (передаём через DI чтобы избежать circular import).
    """
    model = model or os.getenv("LLM_MODEL_SMART") or \
              os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")

    initial_coverage = matrix.coverage
    queries_spent = 0
    all_new_facts: list[Fact] = []
    bank_lookup = {e.bank_slug: e for e in entities}
    core_attr_names = [a.name for a in core_schema]

    current_matrix = matrix
    # КРИТИЧНО: current_facts = старые + новые, иначе coverage считается неверно
    current_facts: list[Fact] = list(initial_facts or [])

    for iteration in range(1, MAX_ITERATIONS + 1):
        if queries_spent >= MAX_QUERIES_TOTAL:
            log.warning("[gap_filler] iter %s: budget exhausted (%s queries)",
                         iteration, queries_spent)
            break

        gaps = _prioritize_gaps(current_matrix, core_schema,
                                   max_n=MAX_QUERIES_PER_ITER)
        if not gaps:
            log.warning("[gap_filler] iter %s: no gaps left", iteration)
            break

        # Bag-limit: не более MAX_QUERIES_TOTAL - queries_spent
        remaining = MAX_QUERIES_TOTAL - queries_spent
        gaps = gaps[:remaining]

        log.warning("[gap_filler] iter %s: %s targeted queries, %s queries left in budget",
                     iteration, len(gaps), remaining)

        # Параллельные targeted gap-fills (sem чтобы не повалить LLM)
        sem = asyncio.Semaphore(3)

        async def _fill_one(bank_slug: str, attr: str) -> list[Fact]:
            async with sem:
                entity = bank_lookup.get(bank_slug)
                if not entity:
                    return []
                attr_label = _humanize_attribute(attr, core_schema)
                return await _targeted_search_and_extract(
                    client, entity, attr_label, attr, core_schema, model,
                )

        tasks = [_fill_one(b, a) for b, a in gaps]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        queries_spent += len(gaps)

        iter_new_facts: list[Fact] = []
        for r in results:
            if r:
                iter_new_facts.extend(r)

        if not iter_new_facts:
            log.warning("[gap_filler] iter %s: 0 new facts — stop", iteration)
            break

        # Пере-маппинг source_idx у новых фактов на global indices
        # (расширяем sources_index если URL новый)
        for f in iter_new_facts:
            f.source_idx = _ensure_source_indexed(f.source_url, sources_index,
                                                     bank_lookup.get(f.entity_bank_slug))
        all_new_facts.extend(iter_new_facts)
        current_facts.extend(iter_new_facts)

        # Пере-построение матрицы для оценки coverage
        # NB: matrix_builder требует ВСЕ facts (старые + новые)
        # Здесь мы не имеем доступа к старым в `current_matrix` — поэтому
        # caller передаст их позже отдельно. Внутри gap_filler мы возвращаем
        # ТОЛЬКО новые facts; caller добавит к старым и пере-построит матрицу.
        # Однако для оценки coverage внутри loop пере-строим temp matrix.
        try:
            temp_matrix = build_matrix_fn(entities, current_facts,
                                              sources_index, core_attr_names)
            new_coverage = temp_matrix.coverage
            gain = new_coverage - current_matrix.coverage
            log.warning("[gap_filler] iter %s: coverage %.0f%% → %.0f%% (+%.0f%%)",
                         iteration, current_matrix.coverage * 100,
                         new_coverage * 100, gain * 100)
            current_matrix = temp_matrix
            if gain < MIN_COVERAGE_GAIN:
                log.warning("[gap_filler] iter %s: gain too small — stop", iteration)
                break
        except Exception as e:
            log.warning("[gap_filler] temp matrix build failed: %s", e)
            break

    total_gain = current_matrix.coverage - initial_coverage
    log.warning("[gap_filler] DONE: +%s facts, +%.0f%% coverage, %s queries",
                 len(all_new_facts), total_gain * 100, queries_spent)
    return all_new_facts, current_matrix


def _ensure_source_indexed(url: str, sources_index: list[dict],
                              entity: Entity | None = None) -> int:
    """Если URL уже в sources_index → вернуть его n.
    Иначе добавить с новым n и вернуть."""
    if not url:
        return 0
    for s in sources_index:
        if s.get("url") == url:
            return s.get("n", 0)
    # Новый source → добавляем в конец
    next_n = max((s.get("n", 0) for s in sources_index), default=0) + 1
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().removeprefix("www.")
    sources_index.append({
        "n": next_n, "url": url,
        "title": url.split("/")[-1][:120],
        "bank_name": entity.bank_name if entity else "",
        "bank_slug": entity.bank_slug if entity else None,
        "domain": host,
        "trust_score": 0.7,
        "source_kind": "gap_filler",
        "fetched_at": None,
        "excerpts": [],
        "document_id": None,
        "gold_score": 0.5,
    })
    return next_n
