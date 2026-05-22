"""Core Schema — определяет 10-15 КЛЮЧЕВЫХ атрибутов для конкретного продукта.

Без этого triple_extractor выдаёт всё подряд (карта-стикер 700₽, дизайн 500₽),
а главные параметры (выпуск, обслуживание, кешбэк, ставка) теряются.

LLM-call на старте: «для продукта X какие 10-15 атрибутов аудитор хочет знать?».
Результат используется как:
1. Подсказка triple_extractor'у что искать в первую очередь
2. Фильтр для главной сравнительной таблицы (только core, не периферия)
3. Якорь для schema_normalizer (канонические имена уже заданы)
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


@dataclass
class CoreAttr:
    name: str          # snake_case canonical, например "годовое_обслуживание"
    label: str         # человекочитаемая метка, "Годовое обслуживание"
    unit: str          # "₽" или "%" или ""
    category: str      # "fee" | "rate" | "limit" | "feature" | "requirement"
    description: str   # короткое объяснение для LLM в extract промпте


SYSTEM_PROMPT = """Ты — банковский продуктовый аналитик. На вход — название
продукта (например «пенсионная карта», «ипотека для семей», «эквайринг для ИП»).
На выход — список 10-15 КЛЮЧЕВЫХ параметров, которые ОБЯЗАТЕЛЬНО хочет
видеть аудитор при сравнении этого продукта между банками.

ПРАВИЛА:
1) Только параметры РЕЛЕВАНТНЫЕ ДАННОМУ ПРОДУКТУ. Для карты — выпуск/обслуживание/
   кешбэк/лимит/процент на остаток. Для ипотеки — ставка/ПВ/срок/сумма.
   Для доверенности — срок_действия/стоимость_оформления/документы.

2) Не более 15 параметров. Лучше меньше, но самые важные.

3) snake_case на русском для name. Понятный human label.

4) Категория ВЫБИРАЕТСЯ из:
   • fee — комиссия / стоимость / тариф (₽)
   • rate — процентная ставка / кешбэк (%)
   • limit — лимит / макс / мин (₽ или операций)
   • feature — функция / возможность (да/нет/список)
   • requirement — требование к клиенту (возраст / документ / справка)

5) ВЫХОД: JSON массив:
[
  {"name":"годовое_обслуживание","label":"Годовое обслуживание","unit":"₽","category":"fee","description":"Плата за обслуживание карты в год"},
  ...
]

БЕЗ преамбулы. БЕЗ markdown-fences."""


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
    candidate = t[start:end] if end > 0 else t[start:].rstrip().rstrip(",") + "]"
    try: return json.loads(candidate)
    except Exception:
        pass
    try: return json.loads(re.sub(r",\s*([\]}])", r"\1", candidate))
    except Exception: return None


async def discover_core_schema(client: AsyncOpenAI, product: str,
                                 audience: str | None = None,
                                 model: str | None = None) -> list[CoreAttr]:
    """Для продукта возвращает 10-15 ключевых атрибутов с категориями."""
    model = model or os.getenv("LLM_MODEL_FAST") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini")
    user = f"# Продукт: {product}"
    if audience:
        user += f"\n# Аудитория: {audience}"
    user += "\n\nВерни JSON массив 10-15 параметров аудитора."

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user},
                ],
                max_tokens=2000, temperature=0.0,
            ), timeout=30,
        )
    except Exception as e:
        log.warning("[core_schema] LLM failed: %s", e)
        return []

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_array(raw)
    if not isinstance(data, list):
        log.warning("[core_schema] no JSON array (raw 200=%r)", raw[:200])
        return []

    out: list[CoreAttr] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip().lower().replace(" ", "_")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(CoreAttr(
            name=name,
            label=(item.get("label") or name.replace("_", " ").capitalize()).strip(),
            unit=(item.get("unit") or "").strip(),
            category=(item.get("category") or "feature").strip().lower(),
            description=(item.get("description") or "").strip(),
        ))
        if len(out) >= 15:
            break
    log.warning("[core_schema] %s × %s → %s core attributes: %s",
                 product, audience or "—", len(out), [a.name for a in out])
    return out


def build_extract_hint(core: list[CoreAttr]) -> str:
    """Форматирует core-схему как блок инструкций для triple_extractor."""
    if not core:
        return ""
    lines = ["", "# ОБЯЗАТЕЛЬНО НАЙДИ значения для этих CORE-атрибутов "
              "(если они есть в источниках):"]
    for a in core:
        lines.append(f"  • {a.name} — {a.label} (категория: {a.category}, "
                       f"единица: {a.unit or '—'}): {a.description}")
    lines.append("Если значения нет — НЕ ВЫДУМЫВАЙ, просто пропусти.")
    lines.append("Периферийные факты (типа стоимости карта-стикера, дизайна) — "
                 "опционально, после core.")
    return "\n".join(lines)


def build_canonical_mapping(core: list[CoreAttr]) -> dict[str, str]:
    """Из core list → mapping (sample variants → canonical name) для
    предсиления schema_normalizer'у. Канонические имена — фиксированные."""
    out: dict[str, str] = {}
    for a in core:
        out[a.name] = a.name   # сам себе canonical
    return out
