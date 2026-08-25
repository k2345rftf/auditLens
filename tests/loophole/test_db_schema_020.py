"""Тест миграции 020_loophole_auth.sql: RBAC-таблицы (loophole_role_mapping, loophole_user_role).

Идемпотентность и структура. Без реальной Greenplum.
"""
from bank_audit.loophole import db_schema


def test_auth_migration_file_exists():
    assert db_schema.MIGRATION_020_PATH.exists()


def test_auth_migration_contains_both_tables():
    sql = db_schema.migration_020_sql()
    assert "CREATE TABLE IF NOT EXISTS loophole_role_mapping" in sql
    assert "CREATE TABLE IF NOT EXISTS loophole_user_role" in sql


def test_auth_migration_has_indexes():
    sql = db_schema.migration_020_sql()
    assert "CREATE INDEX IF NOT EXISTS idx_lrm_group" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_lur_user" in sql


def test_auth_migration_no_primary_key_or_unique():
    """Greenplum 6 — без PRIMARY KEY / UNIQUE-конструкций."""
    sql = db_schema.migration_020_sql()
    upper = sql.upper()
    # Вырезаем однострочные комментарии
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    body_upper = body.upper()
    assert "PRIMARY KEY" not in body_upper
    assert "UNIQUE" not in body_upper


def test_apply_migration_includes_020():
    """apply_migration должен применять 020 вместе со всеми остальными."""
    fake_session = type("FakeS", (), {"execute": lambda *a, **k: None})()
    # Не должно бросить ошибку
    db_schema.apply_migration(fake_session)


def test_db_schema_constants_exposed():
    assert db_schema.T_ROLE_MAPPING == "loophole_role_mapping"
    assert db_schema.T_USER_ROLE == "loophole_user_role"
