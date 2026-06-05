"""Fact — обогащённая единица знания (заменяет плоский Triple).

Главное отличие от старого Triple:
  • conditions      — список условий применения значения
                       («при зачислении пенсии», «при остатке от 30k»)
  • qualifications  — ограничения сегмента
                       («только Premium-клиенты от 5 млн ₽»)
  • exceptions      — исключения
                       («для валютных счетов комиссия 100₽»)
  • verbatim_quote  — дословная цитата 1-2 предложения для narrative-секций
  • page_context    — ±150 chars вокруг цитаты
  • category        — fee/rate/limit/feature/requirement/regulation
  • audit_priority  — high/medium/low (для focus-фильтра)
  • related_attrs   — связи с другими атрибутами (заполняется на normalization)

Без этих полей narrative-генератор пишет плоские bullet-lists вместо
полноценных аналитических текстов уровня demo.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Fact:
    # ── Атомарная информация ────────────────────────────────────────────
    entity_bank_slug: str
    attribute: str             # snake_case canonical, "годовое_обслуживание"
    value: str                 # строковое представление (унификация)
    unit: str = ""             # "₽", "%", "лет", "дней", ""
    value_numeric: float | None = None    # parsed число если возможно

    # ── Контекст и нюансы (КРИТИЧНО для demo-качества) ───────────────────
    conditions: list[str] = field(default_factory=list)
    # Пример: ["при зачислении пенсии", "при остатке от 30k"]
    qualifications: str = ""
    # Пример: "только для Premium-клиентов (от 5 млн ₽)"
    exceptions: list[str] = field(default_factory=list)
    # Пример: ["для валютных счетов комиссия 100₽"]

    # ── Цитирование для narrative ───────────────────────────────────────
    verbatim_quote: str = ""        # 1-2 предложения дословно
    page_context: str = ""          # ±150 chars вокруг (не показывается)

    # ── Семантика ───────────────────────────────────────────────────────
    category: str = "feature"       # fee/rate/limit/feature/requirement/regulation
    audit_priority: str = "medium"  # high/medium/low

    # ── Связи (заполняются на schema_normalizer этапе) ──────────────────
    related_attrs: list[str] = field(default_factory=list)

    # ── Источник ────────────────────────────────────────────────────────
    source_idx: int = 0             # 1-based индекс в global sources
    source_url: str = ""
    confidence: str = "high"        # high/medium/low

    def to_dict(self) -> dict:
        return {
            "bank":          self.entity_bank_slug,
            "attribute":     self.attribute,
            "value":         self.value,
            "unit":          self.unit,
            "value_numeric": self.value_numeric,
            "conditions":    self.conditions,
            "qualifications": self.qualifications,
            "exceptions":    self.exceptions,
            "verbatim_quote": self.verbatim_quote[:400],
            "category":      self.category,
            "audit_priority": self.audit_priority,
            "related_attrs": self.related_attrs,
            "source_idx":    self.source_idx,
            "source_url":    self.source_url,
            "confidence":    self.confidence,
        }

    @property
    def display_value(self) -> str:
        """value+unit для отображения, с учётом condition если есть."""
        s = f"{self.value}".strip()
        if self.unit:
            s = f"{s} {self.unit}".strip()
        if self.conditions:
            s = f"{s} (при условиях)"
        return s

    @property
    def full_value(self) -> str:
        """Полное значение с условиями для narrative-секций."""
        s = self.display_value
        if self.conditions:
            s = f"{s}: {' / '.join(self.conditions)}"
        if self.qualifications:
            s = f"{s} — {self.qualifications}"
        return s


# Маппинг старого Triple → Fact (backward compat)
def triple_to_fact(t) -> Fact:
    """Конвертирует устаревший Triple в Fact (для обратной совместимости)."""
    return Fact(
        entity_bank_slug=t.entity_bank_slug,
        attribute=t.attribute,
        value=t.value,
        unit=t.unit,
        value_numeric=t.value_numeric,
        verbatim_quote=t.excerpt,
        source_idx=t.source_idx,
        source_url=t.source_url,
        confidence=t.confidence,
    )
