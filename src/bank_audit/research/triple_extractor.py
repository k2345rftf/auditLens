"""Triple Extractor — самое сердце EAV-pipeline.

Принимает: Entity + список gold sources.
Возвращает: список троек (attribute, value, unit, source) с цитатами.

Главное: НЕ задаёт схему заранее. LLM сам решает какие attribute'ы у этого
продукта существуют. На эквайринге будут «комиссия за транзакцию»,
на ипотеке — «ставка», на доверенности — «срок действия».

Структура каждой тройки строгая:
  {
    "attribute":     "минимальная_ставка",   # snake_case на русском
    "value":         "6.0",                  # СТРОКА (для унификации)
    "unit":          "%",                    # ед. изм. или ""
    "value_numeric": 6.0,                    # parsed float если число
    "source_idx":    1,                      # 1-based index в gold_sources
    "excerpt":       "...ставка от 6%...",   # 200-300 chars цитата
    "confidence":    "high|medium|low",
  }
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .entity_extractor import Entity
from .source_finder import GoldSource

log = logging.getLogger(__name__)


@dataclass
class Triple:
    """Один факт о entity, привязанный к источнику.

    NB: Triple — это «ячейка матрицы». Раньше при конвертации Fact→Triple
    весь богатый контекст (conditions/qualifications/exceptions/category) терялся,
    из-за чего сравнительная таблица показывала голые числа и условная ставка
    «6,5% при зачислении пенсии» была неотличима от безусловной. Теперь Triple
    ЗЕРКАЛИТ эти поля Fact'а (без хранения самого объекта — чтобы не плодить
    цикл импортов и сохранить тривиальную сериализацию), а для многозначных
    клеток (тарифные лесенки) несёт полный список ступеней в `members`.
    """
    entity_bank_slug: str
    attribute: str             # snake_case ru
    value: str
    unit: str = ""
    value_numeric: float | None = None
    source_idx: int = 0        # 1-based
    source_url: str = ""
    excerpt: str = ""
    confidence: str = "high"   # high/medium/low
    # ── Обогащённый контекст (зеркало Fact; борьба с «голой» таблицей) ──
    conditions: list[str] = field(default_factory=list)
    qualifications: str = ""
    exceptions: list[str] = field(default_factory=list)
    category: str = ""              # fee/rate/limit/feature/requirement/regulation
    audit_priority: str = "medium"  # high/medium/low
    # ── Многозначная клетка (тарифная лесенка/ступени) ──
    # Для клетки со многими значениями одного атрибута здесь лежат ВСЕ ступени
    # (каждая — Triple) с их условиями и цитатами. Рендер показывает лесенку,
    # а не схлопывает её в один врущий диапазон.
    members: list = field(default_factory=list)   # list[Triple]
    is_range: bool = False
    # ── Явное состояние «данные не найдены» (отличается от «нет атрибута») ──
    data_missing: bool = False

    @property
    def has_qualifiers(self) -> bool:
        return bool(self.conditions or self.qualifications or self.exceptions)

    def cell_text(self) -> str:
        """Значение + компактный маркер условности для ячейки таблицы.

        Условная ставка получает видимый маркер «·усл.», чтобы аудитор не
        принял её за безусловную. Полную расшифровку условий даёт сноска/экспорт.
        """
        s = f"{self.value} {self.unit}".strip()
        if self.has_qualifiers:
            s = f"{s} ·усл."
        return s

    def qualifier_text(self) -> str:
        """Расшифровка условий/сегмента/исключений одной строкой (для сноски)."""
        parts: list[str] = []
        if self.conditions:
            parts.append("; ".join(self.conditions))
        if self.qualifications:
            parts.append(self.qualifications)
        if self.exceptions:
            parts.append("исключения: " + "; ".join(self.exceptions))
        return " — ".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return {
            "bank":          self.entity_bank_slug,
            "attribute":     self.attribute,
            "value":         self.value,
            "unit":          self.unit,
            "value_numeric": self.value_numeric,
            "source_idx":    self.source_idx,
            "source_url":    self.source_url,
            "excerpt":       self.excerpt[:300],
            "confidence":    self.confidence,
            "conditions":    self.conditions,
            "qualifications": self.qualifications,
            "exceptions":    self.exceptions,
            "category":      self.category,
            "audit_priority": self.audit_priority,
            "is_range":      self.is_range,
            "members":       [m.to_dict() for m in self.members] if self.members else [],
        }


def _parse_json_array(raw: str) -> list | None:
    """Толерантный парсер JSON-массива (тот же что в entity_extractor)."""
    if not raw:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("[")
    if start < 0:
        return None
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
    candidate = t[start:end] if end > 0 else (t[start:].rstrip().rstrip(",") + "]")
    try:
        return json.loads(candidate)
    except Exception:
        pass
    cleaned = re.sub(r",\s*([\]}])", r"\1", candidate)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _try_parse_numeric(val: str, unit: str) -> float | None:
    """Парсит численное значение если возможно."""
    if not val:
        return None
    # Извлекаем первое число
    m = re.search(r"-?\d+(?:[.,]\d+)?", val)
    if not m:
        return None
    s = m.group(0).replace(",", ".")
    try:
        f = float(s)
    except Exception:
        return None
    # Конверсия единиц: млн → ×1e6, тыс → ×1e3
    ul = (unit or "").lower()
    if "млрд" in ul: f *= 1e9
    elif "млн" in ul: f *= 1e6
    elif "тыс" in ul: f *= 1e3
    return f
