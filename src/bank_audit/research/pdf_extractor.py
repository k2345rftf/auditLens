"""PDF Extractor — text-extraction из PDF тарифных документов банков.

Банки часто прячут полные тарифные ведомости в PDF (Сбер: TP_Sberkarta2.pdf,
ВТБ: tarify_vklada.pdf и т.д.). Без парсинга этих PDF мы видим только
маркетинговую страницу-обложку, что даёт неполную картину.

Использует pdfminer.six (чистый Python, без system deps, лучший результат
с русскими PDF).

Особенности:
  • async-friendly через asyncio.to_thread (pdfminer синхронный)
  • download через httpx (timeout 20s, max 10MB)
  • безопасные fallbacks: пустая строка при ЛЮБОЙ ошибке (не валит pipeline)
  • text-only output (без таблиц-структуры), отдаётся LLM как контекст
  • кеширование результатов (URL → text) — PDF не меняются часто
"""
from __future__ import annotations
import asyncio
import logging
import io
from typing import Optional

import httpx

log = logging.getLogger(__name__)


# Простой in-process кеш (URL → extracted_text). Подходит для одного процесса;
# для multi-worker — можно вынести в Redis/диск.
_PDF_CACHE: dict[str, str] = {}
_PDF_CACHE_MAX = 500   # макс. PDF в памяти


# Ограничения:
MAX_PDF_BYTES   = 10 * 1024 * 1024      # 10 MB
MAX_TEXT_CHARS  = 30_000                 # ~10 страниц
DOWNLOAD_TIMEOUT = 20.0                  # сек
EXTRACT_TIMEOUT  = 25.0                  # сек (pdfminer на больших PDF может тормозить)


def is_pdf_url(url: str) -> bool:
    """Эвристика: URL ведёт на PDF."""
    if not url:
        return False
    u = url.lower().split("?")[0].split("#")[0]
    return u.endswith(".pdf")


def is_pdf_content(content: bytes, content_type: str | None = None) -> bool:
    """Проверка по содержимому: настоящий ли PDF (magic bytes)."""
    if not content or len(content) < 5:
        return False
    if content[:4] == b"%PDF":
        return True
    if content_type and "application/pdf" in content_type.lower():
        return True
    return False


async def _download_pdf(url: str) -> Optional[bytes]:
    """Скачивает PDF, возвращает bytes или None при ошибке."""
    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AuditLensBot/1.0)",
                "Accept": "application/pdf,*/*",
            },
        ) as cli:
            # Streaming чтобы не качать огромные файлы
            async with cli.stream("GET", url) as resp:
                if resp.status_code != 200:
                    log.info("[pdf_extractor] %s → HTTP %s", url, resp.status_code)
                    return None
                ctype = resp.headers.get("content-type", "")
                # Если явно не PDF — рано выходим
                if ctype and "application/pdf" not in ctype.lower() \
                       and not url.lower().endswith(".pdf"):
                    log.info("[pdf_extractor] %s → not PDF (ct=%s)", url, ctype)
                    return None
                # Размер
                size_h = resp.headers.get("content-length")
                if size_h and int(size_h) > MAX_PDF_BYTES:
                    log.warning("[pdf_extractor] %s → too big (%s bytes)",
                                 url, size_h)
                    return None
                # Скачиваем chunk-by-chunk до лимита
                chunks = bytearray()
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    chunks.extend(chunk)
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        log.warning("[pdf_extractor] %s → exceed %s bytes (truncate)",
                                     url, MAX_PDF_BYTES)
                        break
                return bytes(chunks)
    except (httpx.TimeoutException, httpx.ReadError) as e:
        log.info("[pdf_extractor] download timeout/read %s: %s", url, e)
    except Exception as e:
        log.warning("[pdf_extractor] download failed %s: %s", url, e)
    return None


def _extract_text_sync(pdf_bytes: bytes) -> str:
    """Синхронный extract через pdfminer.six. Запускается в to_thread."""
    try:
        # Импорт внутри функции — pdfminer тяжёлый, не загружаем при импорте модуля
        from pdfminer.high_level import extract_text
        from pdfminer.pdfparser import PDFSyntaxError
        text = extract_text(io.BytesIO(pdf_bytes), maxpages=20)
        if not text:
            return ""
        # Чистим whitespace
        import re
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        return text[:MAX_TEXT_CHARS]
    except Exception as e:
        log.info("[pdf_extractor] extract failed: %s", e)
        return ""


async def extract_pdf_text(url: str, use_cache: bool = True) -> str:
    """Главная: URL → extracted text. Возвращает '' при ЛЮБОЙ ошибке.

    Не выбрасывает исключений — pipeline продолжит работать без PDF.
    """
    if not url or not is_pdf_url(url):
        # is_pdf_url — quick reject; для не-pdf URL pdf_extractor не вызывается
        # но на всякий случай — попробуем по magic-bytes после download
        pass

    if use_cache and url in _PDF_CACHE:
        cached = _PDF_CACHE[url]
        log.info("[pdf_extractor] cache hit %s (%s chars)", url, len(cached))
        return cached

    pdf_bytes = await _download_pdf(url)
    if not pdf_bytes:
        if use_cache:
            _cache_put(url, "")
        return ""

    # Проверка magic bytes (на случай если URL не .pdf но content — PDF)
    if not is_pdf_content(pdf_bytes):
        log.info("[pdf_extractor] %s → not PDF by magic bytes", url)
        if use_cache:
            _cache_put(url, "")
        return ""

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_extract_text_sync, pdf_bytes),
            timeout=EXTRACT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning("[pdf_extractor] %s → extract timeout", url)
        text = ""
    except Exception as e:
        log.warning("[pdf_extractor] %s → extract error: %s", url, e)
        text = ""

    if text:
        log.warning("[pdf_extractor] OK %s → %s chars", url, len(text))
    if use_cache:
        _cache_put(url, text)
    return text


def _cache_put(url: str, text: str) -> None:
    if len(_PDF_CACHE) >= _PDF_CACHE_MAX:
        # FIFO eviction: удаляем самый старый
        try:
            first_key = next(iter(_PDF_CACHE))
            del _PDF_CACHE[first_key]
        except StopIteration:
            pass
    _PDF_CACHE[url] = text


async def extract_pdfs_parallel(urls: list[str]) -> dict[str, str]:
    """Параллельный extract нескольких PDF (для batch). Возвращает {url: text}."""
    if not urls:
        return {}
    # Limit concurrency чтобы не задушить downstream
    sem = asyncio.Semaphore(4)

    async def _one(u: str) -> tuple[str, str]:
        async with sem:
            text = await extract_pdf_text(u)
            return u, text

    results = await asyncio.gather(*[_one(u) for u in urls],
                                       return_exceptions=False)
    return dict(results)
