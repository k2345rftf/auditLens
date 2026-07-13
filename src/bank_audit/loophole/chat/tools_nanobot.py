from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text

from .. import repository as repo

log = logging.getLogger(__name__)

_FORBIDDEN = re.compile(
    r"\b(DROP|INSERT|UPDATE|DELETE|ALTER|CREATE|TRUNCATE|GRANT|EXEC|UNION)\b",
    re.IGNORECASE,
)


def _is_read_only_select(sql: str) -> bool:
    """Проверяет, что SQL — только READ-ONLY SELECT."""
    if not sql or not sql.strip().lower().startswith("select"):
        return False
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        return False
    if _FORBIDDEN.search(sql):
        return False
    return True


def db_query(sql: str, *, session: Any = None) -> dict:
    """READ-ONLY SQL-запрос к БД лазеек.

    Возвращает {"columns": [...], "rows": [...], "row_count": int}.
    При ошибке возвращает {"error": str}.
    """
    if not _is_read_only_select(sql):
        return {"error": "only SELECT queries are allowed"}

    # Принудительно ограничиваем LIMIT
    normalized = " ".join(sql.split())
    if "LIMIT" not in normalized.upper():
        sql = f"{sql} LIMIT 500"

    try:
        with repo._session(session) as s:
            result = s.execute(text(sql))
            columns = list(result.keys())
            rows = result.mappings().all()
            return {
                "columns": columns,
                "rows": [list(row.values()) for row in rows],
                "row_count": len(rows),
            }
    except Exception as e:
        log.warning("[db_query] failed: %s", e)
        return {"error": str(e)}
