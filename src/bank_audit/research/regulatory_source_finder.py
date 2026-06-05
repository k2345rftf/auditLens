"""Regulatory Source Finder — поиск НПА и официальных источников.

Параллельно с обычным source_finder, ищет regulatory источники для тем
где topic_classifier определил needs_regulatory=True.

Стратегия:
  1. Берёт regulatory_query_hints из TopicProfile (ЛУЧШЕ всего работает)
  2. Дополняет site:domain queries по regulatory_domains
  3. Ингестит найденные URL'ы в общий pipeline (через ingest_document_from_url
     или прямой fetch + chunking)
  4. Фильтрует ТОЛЬКО whitelisted regulatory domains — никаких coursework

Использует pdf_extractor для PDF-документов (ФЗ обычно в PDF на pravo.gov.ru).

Trust score: 0.95-1.0 для всех regulatory sources.
"""
from __future__ import annotations
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from openai import AsyncOpenAI

from .source_finder import GoldSource
from .topic_classifier import TopicProfile, REGULATORY_DOMAIN_CATALOG
from .pdf_extractor import extract_pdf_text, is_pdf_url
from ..rag.web_search import search as web_search
from ..rag.fetcher import fetch as fetch_url

log = logging.getLogger(__name__)


# ── Plan queries ──────────────────────────────────────────────────────────


def _plan_regulatory_queries(question: str,
                                profile: TopicProfile,
                                max_queries: int = 8) -> list[str]:
    """Генерирует поисковые queries для regulatory."""
    queries: list[str] = []

    # Используем hints из profile (LLM-сгенерированные) — они уже targeted
    for h in profile.regulatory_query_hints[:5]:
        if h and len(h) > 3:
            queries.append(h)

    # Site-specific queries: используем САМЫЙ КОНКРЕТНЫЙ термин из hints
    # (а не сырые слова из вопроса — иначе «Сравни ...» в вопросе путает поисковик)
    topic_terms: list[str] = []
    for h in profile.regulatory_query_hints[:3]:
        # из hint берём первые 3-5 значимых слов (исключая стоп-слова)
        words = re.findall(r"\b[А-Яа-яA-Za-z][А-Яа-яA-Za-z\-]{2,}\b", h)
        stop = {"для", "при", "что", "как", "это", "или", "ст", "пункт", "часть",
                  "сравни", "сравнить", "сравнение", "анализ", "обзор",
                  "условия", "оформления", "оформление"}
        meaningful = [w for w in words if w.lower() not in stop][:4]
        if meaningful:
            topic_terms.append(" ".join(meaningful))

    if not topic_terms:
        # Fallback: продуктовые ключевые слова из question
        words = re.findall(r"\b[А-Яа-яA-Za-z]{5,}\b", question)
        stop2 = {"сравни", "сравнить", "сравнение", "условия", "оформление",
                  "оформления", "банковский", "банковского"}
        meaningful = [w for w in words if w.lower() not in stop2][:3]
        if meaningful:
            topic_terms.append(" ".join(meaningful))

    short_topic = topic_terms[0] if topic_terms else "доверенность"
    for domain in profile.regulatory_domains[:3]:
        queries.append(f"site:{domain} {short_topic}")

    # Дедуп
    seen = set()
    deduped = []
    for q in queries:
        key = q.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)

    return deduped[:max_queries]


# ── Filter URLs ───────────────────────────────────────────────────────────


def _is_regulatory_url(url: str, allowed_domains: set[str]) -> bool:
    """URL должен быть из whitelist regulatory доменов."""
    try:
        host = urlparse(url).netloc.lower()
        host = host.removeprefix("www.")
        # Точное совпадение домена или поддомен
        for ad in allowed_domains:
            if host == ad or host.endswith("." + ad):
                return True
        return False
    except Exception:
        return False


# ── Fetch + extract ───────────────────────────────────────────────────────


async def _fetch_regulatory_url(url: str) -> tuple[str, str] | None:
    """Скачивает regulatory URL и возвращает (text, title). Поддерживает PDF.

    Возвращает None при любой ошибке (pipeline продолжается).
    """
    if not url:
        return None

    # PDF? — извлекаем через pdf_extractor
    if is_pdf_url(url):
        text = await extract_pdf_text(url)
        if not text:
            return None
        title = url.split("/")[-1][:120]
        return text, title

    # HTML — через rag.fetcher
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_url, url),
            timeout=20,
        )
    except Exception as e:
        log.info("[regulatory] fetch %s failed: %s", url, e)
        return None

    if not result or not getattr(result, "content", None):
        return None
    if result.status != 200:
        return None

    # Извлекаем text из HTML (упрощённо через regex)
    try:
        html = result.content.decode("utf-8", errors="replace")
    except Exception:
        return None

    # Простой text-extract: удаляем теги, чистим whitespace
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 200:
        return None
    text = text[:15000]

    # Title из <title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html,
                    flags=re.DOTALL | re.IGNORECASE)
    title = (m.group(1).strip()[:120] if m else url.split("/")[-1][:120])
    title = re.sub(r"\s+", " ", title)

    return text, title


# ── Main ──────────────────────────────────────────────────────────────────


async def find_regulatory_sources(client: AsyncOpenAI,
                                    question: str,
                                    profile: TopicProfile,
                                    top_n: int = 4,
                                    starting_idx: int = 0) -> list[GoldSource]:
    """Возвращает до top_n regulatory sources.

    Если profile.needs_regulatory=False или нет regulatory_domains — пустой list.
    """
    if not profile.needs_regulatory or not profile.regulatory_domains:
        return []

    queries = _plan_regulatory_queries(question, profile, max_queries=6)
    if not queries:
        return []

    log.warning("[regulatory] %s queries for domains %s",
                 len(queries), profile.regulatory_domains)

    allowed_domains = {d.lower() for d in profile.regulatory_domains}

    # 1. Search → collect URLs
    loop = asyncio.get_event_loop()

    def _do_search(q: str) -> list[str]:
        try:
            results = web_search(q, max_results=5) or []
            return [r.get("url", "") for r in results if r.get("url")]
        except Exception as e:
            log.info("[regulatory] search %r failed: %s", q, e)
            return []

    search_tasks = [loop.run_in_executor(None, _do_search, q) for q in queries]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=False)
    all_urls: list[str] = []
    seen_urls: set[str] = set()
    for urls in search_results:
        for u in urls:
            if u in seen_urls:
                continue
            if not _is_regulatory_url(u, allowed_domains):
                continue
            seen_urls.add(u)
            all_urls.append(u)

    log.warning("[regulatory] %s candidate URLs after domain-filter",
                 len(all_urls))

    if not all_urls:
        return []

    # 2. Fetch + extract (parallel, limited concurrency)
    sem = asyncio.Semaphore(3)

    async def _one(url: str) -> GoldSource | None:
        async with sem:
            res = await _fetch_regulatory_url(url)
            if not res:
                return None
            text, title = res
            host = urlparse(url).netloc.lower().removeprefix("www.")
            # Trust из каталога (домен или поддомен)
            trust = 0.95
            for ad, score in REGULATORY_DOMAIN_CATALOG.items():
                if host == ad or host.endswith("." + ad):
                    trust = score
                    break
            return GoldSource(
                url=url, title=title,
                bank_slug=None,
                domain=host,
                trust_score=trust,
                text=text,
                length=len(text),
                gold_score=trust,
                document_id=None,
            )

    fetch_tasks = [_one(u) for u in all_urls[:12]]  # cap candidates
    sources = await asyncio.gather(*fetch_tasks, return_exceptions=False)
    sources = [s for s in sources if s is not None]

    # Rank: trust × text length (длина = больше материала)
    sources.sort(key=lambda s: -(s.trust_score * min(len(s.text), 10000) / 1000))
    top = sources[:top_n]

    log.warning("[regulatory] %s gold sources (from %s domains): %s",
                 len(top), len({s.domain for s in top}),
                 [(s.domain, s.url[-40:]) for s in top])
    return top
