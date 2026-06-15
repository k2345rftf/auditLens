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


def _norm_time(n: float, unit: str) -> tuple[float, str]:
    """Нормализует срок к годам: 60 мес → (5.0, 'лет'). Иначе возвращает как есть."""
    u = (unit or "").lower()
    if "мес" in u:
        return n / 12.0, "лет"
    if any(x in u for x in ("год", "лет", "года")):
        return n, "лет"
    return n, (unit or "")


def _fmt_num(n: float, unit: str) -> str:
    """Число → строка: целые с разделением тысяч (для ₽), дроби — с запятой."""
    if abs(n - round(n)) < 1e-9:
        n = int(round(n))
        if unit == "₽" or n >= 10000:
            return f"{n:,}".replace(",", " ")
        return str(n)
    return f"{n:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def _aggregate_cell(base: Triple, group: list[Triple]) -> tuple[Triple, bool]:
    """Сводит несколько значений ОДНОГО атрибута одного банка в одну ячейку,
    НЕ теряя информацию.

    Три случая:
      • ОДНО значение → как есть.
      • СТУПЕНИ/РЕЖИМЫ (значения с РАЗНЫМИ условиями: база/промо/для зарплатных)
        → отображаем опорное (base), а ВСЕ ступени кладём в `.members` для
        явного рендера лесенки. НЕ диапазон, НЕ конфликт.
      • ЧИСЛОВОЙ РАЗБРОС без различающих условий (одинаковый _cond_key) →
        честный диапазон min–max, тоже с `.members`.

    Раньше любые несколько значений схлопывались в min–max от group[0], теряя
    промежуточные ступени, их условия и цитаты. Теперь ничего не теряется.
    Возвращает (display_triple, is_range)."""
    members = list(group)

    def _with_members(disp: Triple, is_range: bool) -> tuple[Triple, bool]:
        disp.members = members
        disp.is_range = is_range
        return disp, is_range

    if len(group) < 2:
        # Единичный срок в месяцах, кратный 12 → показываем в годах (96 мес → 8 лет)
        u = (base.unit or "").lower()
        if base.value_numeric is not None and "мес" in u:
            m = base.value_numeric
            if m >= 12 and abs(m / 12 - round(m / 12)) < 1e-9:
                yrs = int(round(m / 12))
                conv = Triple(
                    entity_bank_slug=base.entity_bank_slug, attribute=base.attribute,
                    value=str(yrs), unit="лет", value_numeric=float(yrs),
                    source_idx=base.source_idx, source_url=base.source_url,
                    excerpt=base.excerpt, confidence=base.confidence,
                    conditions=base.conditions, qualifications=base.qualifications,
                    exceptions=base.exceptions, category=base.category,
                    audit_priority=base.audit_priority,
                )
                return conv, False
        return base, False

    # СТУПЕНИ: если у значений РАЗНЫЕ условия — это лесенка/режимы, не диапазон.
    distinct_conds = {_cond_key(g) for g in group}
    if len(distinct_conds) > 1:
        # base уже самый приоритетный (group отсортирован по confidence в build).
        return _with_members(base, False)

    # ОДИНАКОВЫЕ условия, но РАЗНЫЕ источники и МАТЕРИАЛЬНО разные значения →
    # это РАСХОЖДЕНИЕ источников (15% ↔ 20%), а НЕ диапазон. Не сглаживаем в
    # «15–20», показываем опорное значение; build пометит клетку конфликтом.
    distinct_urls = {g.source_url for g in group if g.source_url}
    if len(distinct_urls) > 1 and _is_material_conflict(group):
        return _with_members(base, False)

    # Иначе — однородный числовой разброс из одного источника → честный диапазон.
    norm = []
    for g in group:
        if g.value_numeric is None:
            continue
        v, u = _norm_time(g.value_numeric, g.unit or "")
        norm.append((v, u))
    if len(norm) < 2:
        return _with_members(base, False)
    units = {u for _, u in norm}
    if len(units) != 1:                 # несовместимые единицы — не диапазон
        return _with_members(base, False)
    unit = next(iter(units))
    vals = [v for v, _ in norm]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:                   # все значения равны
        return _with_members(base, False)
    rng = f"{_fmt_num(lo, unit)}–{_fmt_num(hi, unit)}"
    disp = Triple(
        entity_bank_slug=base.entity_bank_slug, attribute=base.attribute,
        value=rng, unit=unit, value_numeric=(lo + hi) / 2,
        source_idx=base.source_idx, source_url=base.source_url,
        excerpt=base.excerpt, confidence=base.confidence,
        conditions=base.conditions, qualifications=base.qualifications,
        exceptions=base.exceptions, category=base.category,
        audit_priority=base.audit_priority,
    )
    return _with_members(disp, True)


def _is_conflict(group: list[Triple]) -> bool:
    """Конфликт = в пределах ОДНОГО режима (одинаковые условия) РАЗНЫЕ источники
    дают МАТЕРИАЛЬНО разные значения (15% ↔ 20%). Разные условия (база/промо) —
    НЕ конфликт, это легитимная лесенка. Разброс из ОДНОГО источника трактуем как
    диапазон, а не противоречие (иначе любая «6–12%» помечалась бы конфликтом)."""
    by_cond: dict[str, list[Triple]] = defaultdict(list)
    for g in group:
        by_cond[_cond_key(g)].append(g)
    for _, gs in by_cond.items():
        if len(gs) > 1 and _is_material_conflict(gs):
            distinct_urls = {g.source_url for g in gs if g.source_url}
            if len(distinct_urls) > 1:
                return True
    return False


def _is_material_conflict(group) -> bool:
    """True только если значения РЕАЛЬНО противоречат друг другу.

    Не конфликт (шум):
      • одинаковое число с разными префиксами («до 12.5%» ↔ «12.5%»);
      • числа в пределах ~2% (округление);
      • одно значение — уточнённая версия другого (substring после нормализации);
      • «да»/«есть»/«true» — синонимы наличия.
    Конфликт: разные числа (4% ↔ 13.5%) или противоположные тексты.
    """
    # 1) Числовое сравнение, если у всех есть value_numeric.
    #    Время нормализуем к годам (5 лет и 60 мес — НЕ конфликт).
    nums = []
    for g in group:
        if g.value_numeric is None:
            continue
        v, _u = _norm_time(g.value_numeric, g.unit or "")
        nums.append(v)
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
    """Конвертер Fact → Triple для ячейки матрицы.

    КЛЮЧЕВАЯ ПРАВКА: больше НЕ теряем богатый контекст. conditions/
    qualifications/exceptions/category/audit_priority зеркалятся на Triple, чтобы
    сравнительная таблица и экспорт могли показать, ЧЕМ обусловлено значение
    (условная ставка ≠ безусловная). Самое важное в сравнении банков сохраняется.
    """
    return Triple(
        entity_bank_slug=f.entity_bank_slug,
        attribute=f.attribute,
        value=f.value, unit=f.unit, value_numeric=f.value_numeric,
        source_idx=f.source_idx, source_url=f.source_url,
        excerpt=f.verbatim_quote, confidence=f.confidence,
        conditions=list(f.conditions or []),
        qualifications=f.qualifications or "",
        exceptions=list(f.exceptions or []),
        category=f.category or "",
        audit_priority=f.audit_priority or "medium",
    )


def _cond_key(t: Triple) -> str:
    """Ключ режима/ступени: нормализованные условия+сегмент. Разные условия =
    разные ступени (база/промо/для зарплатных), НЕ конфликт источников."""
    conds = "|".join(sorted(_norm_val(c) for c in (t.conditions or [])))
    return conds + "##" + _norm_val(t.qualifications or "")


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
    # Банки, по которым НЕ найдено реальных данных (источник не прочитан/блок).
    # Их пустые клетки рендерятся как «нет данных — источник не прочитан»,
    # а НЕ «банк не предлагает». Это разные сигналы для аудитора.
    insufficient_banks: set = field(default_factory=set)

    def cell(self, bank_slug: str, attribute: str) -> Triple | None:
        return self.cells.get((bank_slug, attribute))

    def null_cells(self) -> list[tuple[str, str]]:
        """Список (bank, attr) пустых клеток — для gap-filler."""
        return [k for k, v in self.cells.items() if v is None]

    def bank_coverage(self, bank_slug: str, core_attrs: list[str] | None = None) -> float:
        """Покрытие по одному банку (доля заполненных core-атрибутов).

        Нужно для пер-банковой симметрии: gap-filling и добор источников должны
        запускаться по ОТСТАЮЩЕМУ банку, а не по среднему по матрице."""
        attrs = [a for a in (core_attrs or self.attributes)] or self.attributes
        if not attrs:
            return 0.0
        filled = sum(1 for a in attrs if self.cells.get((bank_slug, a)) is not None)
        return filled / len(attrs)


def _compute_variance(cells: dict[tuple[str, str], Triple | None],
                       attributes: list[str], banks: list[str]) -> list[tuple[str, float]]:
    """Для каждого attribute считает «насколько банки отличаются».
    Полезный сигнал: атрибуты с высокой variance — главное содержание отчёта.
    Формула: для числовых — coefficient of variation, для строковых — кол-во разных значений.
    Возвращает [(attribute, variance_score)] отсортированный по убыванию."""
    _AFFIRM = {"да", "есть", "true", "yes", "+", "доступно", "возможно", "нет",
               "false", "no", "-", "недоступно"}
    out = []
    for attr in attributes:
        values = []
        for bank in banks:
            t = cells.get((bank, attr))
            # Плейсхолдеры/«не найден» НЕ участвуют в variance — иначе «есть/нет/
            # не найден» давали ложную variance=1.0 и тащили мусор в графики.
            if not _is_real_cell(t):
                continue
            if t.value_numeric is not None:
                values.append(t.value_numeric)
            else:
                nv = _norm_val(t.value)
                values.append("__affirm__" if nv in _AFFIRM else nv)
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


def _is_real_cell(t: Triple | None) -> bool:
    """Клетка несёт реальные данные (не плейсхолдер «не найден», не data_missing)."""
    if t is None or getattr(t, "data_missing", False):
        return False
    v = (t.value or "").lower()
    if "не найден" in v or "не найдены источ" in v:
        return False
    return True


def build_matrix(entities: list[Entity],
                   triples: list[Triple] | list[Fact],
                   sources_index: list[dict] | None = None,
                   core_attrs: list[str] | None = None,
                   insufficient_banks: set | None = None) -> Matrix:
    """Собирает матрицу.

    triples            — Triple ИЛИ Fact (автоматически конвертирует Fact→Triple)
    sources_index      — глобальный список источников с n-маркерами
    insufficient_banks — банки без реальных данных (пустые клетки → «нет данных»)
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
    # ВСЕ core-атрибуты — колонки таблицы, ДАЖЕ полностью пустые. Иначе атрибут,
    # которого нет ни у одного банка, был невидим в таблице И выпадал из
    # знаменателя покрытия (завышая coverage). Теперь «срок: ⚠ Не раскрыто»
    # честно виден, а покрытие считается по полному core.
    for a in (core_attrs or []):
        if a not in attrs_seen:
            attrs_seen[a] = 0
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
        # Опорное значение: высший confidence, при равенстве — high audit_priority.
        _PRIO = {"high": 2, "medium": 1, "low": 0}
        group.sort(key=lambda x: (-_CONF_RANK.get(x.confidence, 1),
                                    -_PRIO.get(x.audit_priority, 1)))
        # Multi-value: ступени/режимы → лесенка в .members; разброс → диапазон.
        display, is_range = _aggregate_cell(group[0], group)
        cells[key] = display
        # Конфликт = в пределах ОДНОГО режима источники материально расходятся.
        # Лесенка (разные условия) и диапазон — НЕ конфликт. Требование «разные
        # URL» снято: источник может противоречить сам себе (item 40).
        if len(group) > 1 and not is_range and _is_conflict(group):
            conflicts[key] = group

    # Coverage — ЧЕСТНАЯ метрика по CORE-атрибутам.
    # Периферийные атрибуты, найденные gap_filler'ом (карта-стикер, бонусы),
    # не должны раздувать знаменатель и занижать coverage. Если core_attrs
    # заданы — меряем «сколько ключевых параметров заполнено», иначе по всем.
    cov_attrs = [a for a in (core_attrs or []) if a in attributes]
    if not cov_attrs:
        cov_attrs = list(attributes)
    total = len(banks) * len(cov_attrs)
    # ЧЕСТНО: плейсхолдеры «не найден»/data_missing НЕ считаются заполненными.
    filled = sum(1 for b in banks for a in cov_attrs
                  if _is_real_cell(cells.get((b, a))))
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
        insufficient_banks=set(insufficient_banks or set()),
    )
    # Сохраняем список core attributes для renderer'а (главная таблица показывает их)
    setattr(matrix, "core_attrs", list(core_attrs or []))
    return matrix
