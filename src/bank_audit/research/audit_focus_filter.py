"""Audit Focus Filter — фильтрация фактов перед narrative-генерацией.

Зачем: фактов после extraction много (30-50+ на pipeline), но narrative
страдает когда туда попадают low-priority периферийные («дизайн карты»).
Этот фильтр убирает шум перед передачей в narrative-генераторы.

ВАЖНО: матрица сохраняет ВСЕ факты, фильтр применяется ТОЛЬКО к facts
которые идут в narrative-генераторы. Это сохраняет полноту таблицы и
коэффициенты coverage.
"""
from __future__ import annotations
import logging
from .fact import Fact

log = logging.getLogger(__name__)


# Категории факта которые ВСЕГДА оставляем (даже low-priority)
ALWAYS_KEEP_CATEGORIES = {"requirement", "regulation"}


def filter_for_narrative(facts: list[Fact], mode: str = "auditor") -> list[Fact]:
    """Фильтрует факты для narrative-генерации.

    mode:
      • "auditor"        — все high + medium + low с категорией requirement/regulation
      • "comprehensive"  — все факты (no filter)
      • "executive"      — только high

    Возвращает отфильтрованный список (исходный не модифицирует).
    """
    if not facts:
        return []

    if mode == "comprehensive":
        return list(facts)

    if mode == "executive":
        return [f for f in facts if f.audit_priority == "high"]

    # Default: "auditor"
    out = []
    for f in facts:
        prio = (f.audit_priority or "medium").lower()
        cat = (f.category or "feature").lower()
        if prio in ("high", "medium"):
            out.append(f)
            continue
        if cat in ALWAYS_KEEP_CATEGORIES:
            out.append(f)
            continue
        # low + не критичная категория → отбрасываем
    n_drop = len(facts) - len(out)
    if n_drop > 0:
        log.warning("[audit_focus] dropped %s low-priority feature facts "
                     "(narrative input: %s/%s)", n_drop, len(out), len(facts))
    return out
