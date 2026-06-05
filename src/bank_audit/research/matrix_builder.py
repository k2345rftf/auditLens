"""Matrix Builder — собирает 2D-матрицу entities × attributes из триплов.

Каждая клетка матрицы: либо Triple (если найден факт), либо None (null = gap).

Дополнительно вычисляет статистики:
  • coverage: процент заполненных клеток
  • variance: какие атрибуты дают наибольшее различие между банками
  • conflicts: где у одного банка несколько разных значений для атрибута
"""
from __future__ import annotations
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .entity_extractor import Entity
from .triple_extractor import Triple
from .fact import Fact

log = logging.getLogger(__name__)


def _norm_val(s: str) -> str:
    """Нормализация значения для сравнения: lower, убираем до/от/~/около, пробелы."""
    import re as _re
    s = (s or "").lower().strip()
    s = _re.sub(r"\b(до|от|примерно|около|~|более|менее|свыше)\b", "", s)
    s = _re.sub(r"\s+", " ", s).strip(" .,:;")
    return s


def _is_material_conflict(group) -> bool:
    """True только если значения РЕАЛЬНО противоречат друг другу.

    Не конфликт (шум):
      • одинаковое число с разными префиксами («до 12.5%» ↔ «12.5%»);
      • числа в пределах ~2% (округление);
      • одно значение — уточнённая версия другого (substring после нормализации);
      • «да»/«есть»/«true» — синонимы наличия.
    Конфликт: разные числа (4% ↔ 13.5%) или противоположные тексты.
    """
    # 1) Числовое сравнение, если у всех есть value_numeric
    nums = [g.value_numeric for g in group if g.value_numeric is not None]
    if len(nums) >= 2 and len(nums) == len([g for g in group if g.value is not None]):
        lo, hi = min(nums), max(nums)
        if lo == 0:
            return hi != 0 and abs(hi) > 0.001
        return (hi - lo) / abs(lo) > 0.02   # >2% разницы = реальный конфликт
    # 2) Текстовое сравнение
    _AFFIRM = {"да", "есть", "true", "yes", "+", "доступно", "возможно"}
    norms = []
    for g in group:
        nv = _norm_val(g.value)
        if nv in _AFFIRM:
            nv = "__affirm__"
        norms.append(nv)
    uniq = [n for n in set(norms) if n]
    if len(uniq) <= 1:
        return False
    # если одно значение — подстрока другого (уточнение), не конфликт
    for a in uniq:
        for b in uniq:
            if a != b and a in b:
                # есть пара «уточнение» — проверим, все ли так связаны
                pass
    # конфликт, если есть хотя бы две взаимно-НЕ-вложенные строки
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            if a not in b and b not in a:
                return True
    return False


def _fact_to_triple(f: Fact) -> Triple:
    """Конвертер Fact → Triple для совместимости с матрицей.

    Сохраняем основные поля. Богатый контекст (conditions/qualifications/
    exceptions/verbatim_quote/category/audit_priority) теряется в матрице
    но остаётся доступным через исходный список фактов в narrative-секциях.
    """
    return Triple(
        entity_bank_slug=f.entity_bank_slug,
        attribute=f.attribute,
        value=f.value, unit=f.unit, value_numeric=f.value_numeric,
        source_idx=f.source_idx, source_url=f.source_url,
        excerpt=f.verbatim_quote, confidence=f.confidence,
    )


@dataclass
class Matrix:
    """Результат: банки × атрибуты + метаданные."""
    entities:    list[Entity]                            # rows
    attributes:  list[str]                                # columns (canonical names)
    cells:       dict[tuple[str, str], Triple | None]    # (bank_slug, attr) → Triple or None
    conflicts:   dict[tuple[str, str], list[Triple]]     # cells with >1 triple
    coverage:    float = 0.0                              # 0..1
    variance:    list[tuple[str, float]] = field(default_factory=list)
    # Прокинуть source-map для рендера цитат
    sources:     list[dict] = field(default_factory=list)   # [{n, url, title, ...}]

    def cell(self, bank_slug: str, attribute: str) -> Triple | None:
        return self.cells.get((bank_slug, attribute))

    def null_cells(self) -> list[tuple[str, str]]:
        """Список (bank, attr) пустых клеток — для gap-filler."""
        return [k for k, v in self.cells.items() if v is None]


def _compute_variance(cells: dict[tuple[str, str], Triple | None],
                       attributes: list[str], banks: list[str]) -> list[tuple[str, float]]:
    """Для каждого attribute считает «насколько банки отличаются».
    Полезный сигнал: атрибуты с высокой variance — главное содержание отчёта.
    Формула: для числовых — coefficient of variation, для строковых — кол-во разных значений.
    Возвращает [(attribute, variance_score)] отсортированный по убыванию."""
    out = []
    for attr in attributes:
        values = []
        for bank in banks:
            t = cells.get((bank, attr))
            if t is None:
                continue
            if t.value_numeric is not None:
                values.append(t.value_numeric)
            else:
                values.append(t.value.lower().strip())
        if not values:
            out.append((attr, 0.0))
            continue
        if all(isinstance(v, (int, float)) for v in values):
            # Coefficient of variation
            if len(values) < 2:
                score = 0.0
            else:
                mean = sum(values) / len(values)
                if mean == 0:
                    score = 0.0
                else:
                    var = sum((v - mean) ** 2 for v in values) / len(values)
                    score = (var ** 0.5) / abs(mean)
        else:
            score = len(set(str(v) for v in values)) / max(1, len(values))
        out.append((attr, round(score, 3)))
    out.sort(key=lambda x: -x[1])
    return out


def build_matrix(entities: list[Entity],
                   triples: list[Triple] | list[Fact],
                   sources_index: list[dict] | None = None,
                   core_attrs: list[str] | None = None) -> Matrix:
    """Собирает матрицу.

    triples         — Triple ИЛИ Fact (автоматически конвертирует Fact→Triple)
    sources_index   — глобальный список источников с n-маркерами
    """
    # Backward compat: если передали Fact[] — конвертируем в Triple[]
    if triples and isinstance(triples[0], Fact):
        triples = [_fact_to_triple(t) if isinstance(t, Fact) else t
                    for t in triples]
    banks = [e.bank_slug for e in entities]
    # Собираем все уникальные attribute'ы (уже canonical после schema_normalizer)
    attrs_seen: dict[str, int] = defaultdict(int)
    for t in triples:
        attrs_seen[t.attribute] += 1
    # Сортируем атрибуты: 1) core_attrs всегда первыми (в порядке их priority)
    # 2) затем частые в нескольких банках 3) затем алфавит.
    core_set = set(core_attrs or [])
    core_order = {a: i for i, a in enumerate(core_attrs or [])}
    attributes = sorted(
        attrs_seen.keys(),
        key=lambda a: (
            a not in core_set,        # core первыми
            core_order.get(a, 9999),  # порядок внутри core
            -attrs_seen[a],            # частые выше
            a                          # алфавит
        )
    )

    # Заполняем cells. Если у одного банка несколько триплов одного attribute —
    # берём с высшим confidence, остальные → conflicts.
    cells: dict[tuple[str, str], Triple | None] = {}
    conflicts: dict[tuple[str, str], list[Triple]] = {}
    grouped: dict[tuple[str, str], list[Triple]] = defaultdict(list)
    for t in triples:
        grouped[(t.entity_bank_slug, t.attribute)].append(t)
    # Инициализируем все клетки как None
    for bank in banks:
        for attr in attributes:
            cells[(bank, attr)] = None
    # Заполняем
    _CONF_RANK = {"high": 3, "medium": 2, "low": 1}
    for key, group in grouped.items():
        if not group:
            continue
        group.sort(key=lambda x: -_CONF_RANK.get(x.confidence, 1))
        cells[key] = group[0]
        if len(group) > 1:
            # Конфликт ТОЛЬКО если значения МАТЕРИАЛЬНО различаются и из РАЗНЫХ
            # источников. Отсекаем шум: «до 12.5%»↔«12.5%» (то же число),
            # «ежемесячно»↔«ежемесячно в последний день» (уточнение, не противоречие).
            distinct_urls = {g.source_url for g in group}
            if len(distinct_urls) > 1 and _is_material_conflict(group):
                conflicts[key] = group

    # Coverage — ЧЕСТНАЯ метрика по CORE-атрибутам.
    # Периферийные атрибуты, найденные gap_filler'ом (карта-стикер, бонусы),
    # не должны раздувать знаменатель и занижать coverage. Если core_attrs
    # заданы — меряем «сколько ключевых параметров заполнено», иначе по всем.
    cov_attrs = [a for a in (core_attrs or []) if a in attributes]
    if not cov_attrs:
        cov_attrs = list(attributes)
    total = len(banks) * len(cov_attrs)
    filled = sum(1 for b in banks for a in cov_attrs
                  if cells.get((b, a)) is not None)
    coverage = (filled / total) if total > 0 else 0.0

    # Variance
    variance = _compute_variance(cells, attributes, banks)

    log.warning("[matrix_builder] %s entities × %s attrs (%s core) = %s core-cells, %s filled (%.0f%%), %s conflicts",
                 len(banks), len(attributes), len(cov_attrs), total, filled,
                 coverage * 100, len(conflicts))
    matrix = Matrix(
        entities=entities,
        attributes=attributes,
        cells=cells,
        conflicts=conflicts,
        coverage=coverage,
        variance=variance,
        sources=sources_index or [],
    )
    # Сохраняем список core attributes для renderer'а (главная таблица показывает их)
    setattr(matrix, "core_attrs", list(core_attrs or []))
    return matrix
