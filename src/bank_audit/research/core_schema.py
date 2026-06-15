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

2) Перечисли ВСЕ существенные для аудита параметры этого продукта (обычно
   12-20). Не урезай искусственно — лучше полный список ключевых параметров,
   чем потерять важную колонку сравнения. Но и не плоди мусор: только то, что
   аудитор реально сравнивает между банками.

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


# Потолок числа core-атрибутов — НЕ жёсткие 15 (это занижало полноту таблицы).
# Конфигурируется; пол MIN гарантирует, что схема не выродится в 2-3 колонки.
CORE_SCHEMA_MAX = int(os.getenv("CORE_SCHEMA_MAX", "20") or 20)
CORE_SCHEMA_MIN = int(os.getenv("CORE_SCHEMA_MIN", "8") or 8)


def _parse_core_items(data, cap: int) -> list[CoreAttr]:
    out: list[CoreAttr] = []
    seen: set[str] = set()
    for item in data or []:
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
        if len(out) >= cap:
            break
    return out


async def discover_core_schema(client: AsyncOpenAI, product: str,
                                 audience: str | None = None,
                                 model: str | None = None) -> list[CoreAttr]:
    """Для продукта возвращает 12-20 ключевых атрибутов с категориями.

    Floor: если первый вызов дал <MIN атрибутов (LLM поскупился/сбой парсинга) —
    один повтор с настойчивой инструкцией. Это потолок полноты всей таблицы,
    поэтому пустой/тонкий результат недопустим."""
    model = model or os.getenv("LLM_MODEL_FAST") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini")
    base_user = f"# Продукт: {product}"
    if audience:
        base_user += f"\n# Аудитория: {audience}"

    async def _ask(extra: str) -> list[CoreAttr]:
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": base_user + extra},
                    ],
                    max_tokens=2600, temperature=0.0,
                ), timeout=40,
            )
        except Exception as e:
            log.warning("[core_schema] LLM failed: %s", e)
            return []
        raw = (resp.choices[0].message.content or "").strip()
        data = _parse_array(raw)
        if not isinstance(data, list):
            log.warning("[core_schema] no JSON array (raw 200=%r)", raw[:200])
            return []
        return _parse_core_items(data, CORE_SCHEMA_MAX)

    out = await _ask("\n\nВерни JSON массив 12-20 параметров аудитора (полно).")
    if len(out) < CORE_SCHEMA_MIN:
        log.warning("[core_schema] только %s атрибутов (<%s) → повтор с нажимом",
                     len(out), CORE_SCHEMA_MIN)
        retry = await _ask(
            f"\n\nПервый список был НЕПОЛНЫМ. Перечисли НЕ МЕНЕЕ {CORE_SCHEMA_MIN}-15 "
            f"ключевых параметров этого продукта (ставки, комиссии, лимиты, "
            f"требования, сроки, условия). Верни ПОЛНЫЙ JSON массив.")
        # берём более полный из двух
        if len(retry) > len(out):
            out = retry

    log.warning("[core_schema] %s × %s → %s core attributes: %s",
                 product, audience or "—", len(out), [a.name for a in out])
    return out


# Категория атрибута по эвристике (для derive-fallback). Минимум хардкода:
# только грубое сопоставление единиц/ключевых слов, когда LLM-схемы нет вообще.
def _guess_category(attr: str, unit: str) -> str:
    a, u = attr.lower(), (unit or "").lower()
    if "%" in u:
        return "rate"
    if "₽" in u or "руб" in u:
        return "fee" if any(k in a for k in ("комисс", "обслуж", "плат", "стоим", "тариф")) else "limit"
    if any(k in a for k in ("ставк", "процент", "кешбэк", "кэшбэк", "доходн")):
        return "rate"
    if any(k in a for k in ("лимит", "сумм", "макс", "мин")):
        return "limit"
    if any(k in a for k in ("документ", "возраст", "доход", "требован", "стаж")):
        return "requirement"
    return "feature"


def derive_core_from_facts(facts, k: int = CORE_SCHEMA_MAX) -> list[CoreAttr]:
    """Аварийный вывод core-схемы из УЖЕ извлечённых фактов, когда LLM-discovery
    вернул пусто. Берём атрибуты, встречающиеся у наибольшего числа банков
    (и приоритетные), чтобы сравнительная таблица не оказалась пустой.

    Без этого пустой core → пустая таблица и пустой знаменатель покрытия."""
    from collections import defaultdict
    banks_by_attr: dict[str, set] = defaultdict(set)
    prio_by_attr: dict[str, int] = defaultdict(int)
    unit_by_attr: dict[str, str] = {}
    _PRIO = {"high": 2, "medium": 1, "low": 0}
    for f in facts or []:
        attr = getattr(f, "attribute", "")
        if not attr or attr == "продукт_доступен":
            continue
        banks_by_attr[attr].add(getattr(f, "entity_bank_slug", ""))
        prio_by_attr[attr] = max(prio_by_attr[attr], _PRIO.get(getattr(f, "audit_priority", "medium"), 1))
        unit_by_attr.setdefault(attr, getattr(f, "unit", "") or "")
    # ранжируем: сначала по числу банков (общность), затем по приоритету
    ranked = sorted(banks_by_attr.keys(),
                    key=lambda a: (-len(banks_by_attr[a]), -prio_by_attr[a], a))
    out: list[CoreAttr] = []
    for attr in ranked[:k]:
        out.append(CoreAttr(
            name=attr,
            label=attr.replace("_", " ").capitalize(),
            unit=unit_by_attr.get(attr, ""),
            category=_guess_category(attr, unit_by_attr.get(attr, "")),
            description="",
        ))
    log.warning("[core_schema] derive_core_from_facts → %s атрибутов", len(out))
    return out


def build_extract_hint(core: list[CoreAttr]) -> str:
    """ЗАКРЫТЫЙ СПИСОК СЛОТОВ для извлечения (контракт slot_id).

    Главная правка против «пустой таблицы»: extractor НЕ должен придумывать
    синонимы имён — он обязан класть факт РОВНО в один из slot_id этого списка.
    Тогда matrix джойнит ячейки по стабильному enum, а не по совпадению свободных
    строк трёх независимых LLM-вызовов (и schema_normalizer становится не нужен).
    """
    if not core:
        return ""
    lines = ["",
             "# ЗАКРЫТЫЙ СПИСОК СЛОТОВ (slot_id). Поле \"attribute\" КАЖДОГО факта",
             "# обязано быть РОВНО одним из этих slot_id — копируй дословно, БЕЗ",
             "# переименований и синонимов:"]
    for a in core:
        desc = f" — {a.description}" if a.description else ""
        lines.append(f"  • {a.name}  ({a.label}; {a.category}; {a.unit or '—'}){desc}")
    lines.append("")
    lines.append("ПРАВИЛА СЛОТОВ:")
    lines.append("  1) Нашёл значение параметра из списка → attribute = его slot_id ДОСЛОВНО. "
                 "ЗАПРЕЩЕНО переименовывать (слот 'комиссия_за_операцию' — НЕ пиши "
                 "'комиссия_за_перевод').")
    lines.append("  2) Несколько режимов одного слота (база/промо/для зарплатных) → "
                 "НЕСКОЛЬКО фактов с ОДНИМ И ТЕМ ЖЕ slot_id, различие — в conditions.")
    lines.append("  3) Материальный факт, не подходящий НИ В ОДИН слот → новый короткий "
                 "snake_case attribute (это исключение, не норма).")
    lines.append("  4) Значения нет в источнике → НЕ выдумывай, просто пропусти слот.")
    return "\n".join(lines)
