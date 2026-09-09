"""Миграция типа находки кандидата: offline текстовый контракт и staging PG."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "067_loophole_candidate_finding_type.sql"
)


def test_migration_text_declares_idempotent_column_and_check():
    """Offline-контракт миграции 067 по тексту файла (без PostgreSQL)."""
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS finding_type" in migration
    assert "DEFAULT 'loophole'" in migration
    assert "CHECK (finding_type IN ('loophole', 'fraud_scheme'))" in migration
    # CHECK добивается отдельным идемпотентным блоком на случай колонки,
    # созданной ранее без констрейнта (ручной хот-фикс, частичный накат).
    assert "DO $$" in migration
    assert "pg_constraint" in migration
    assert "IF NOT EXISTS" in migration


def test_finding_type_migration_defaults_and_is_idempotent():
    staging_url = os.getenv("AUDITLENS_POSTGRES_STAGING_URL")
    if not staging_url:
        pytest.skip("Для проверки миграции нужен явный PostgreSQL staging")
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    connection = psycopg.connect(
        staging_url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=5,
    )
    try:
        connection.execute("SET LOCAL search_path TO pg_temp")
        connection.execute("""
            CREATE TEMP TABLE loophole_research_candidate (
                candidate_id INTEGER PRIMARY KEY, research_id INTEGER, source_id INTEGER,
                title TEXT, is_loophole BOOLEAN
            );
            INSERT INTO loophole_research_candidate
                (candidate_id, research_id, source_id, title, is_loophole)
            VALUES (1, 1, 1, 'Лазейка', TRUE), (2, 1, 1, 'Схема', TRUE);
        """)
        # Повторный накат идемпотентен, исторические строки получают дефолт.
        for _ in range(2):
            connection.execute(migration)
            assert connection.execute(
                "SELECT finding_type FROM loophole_research_candidate ORDER BY candidate_id"
            ).fetchall() == [("loophole",), ("loophole",)]
        connection.execute(
            "UPDATE loophole_research_candidate SET finding_type = 'fraud_scheme' "
            "WHERE candidate_id = 2"
        )
        connection.execute(migration)
        assert connection.execute(
            "SELECT finding_type FROM loophole_research_candidate WHERE candidate_id = 2"
        ).fetchone() == ("fraud_scheme",)
        with pytest.raises(psycopg.IntegrityError), connection.transaction():
            connection.execute(
                "INSERT INTO loophole_research_candidate "
                "(candidate_id, research_id, source_id, title, is_loophole, finding_type) "
                "VALUES (3, 1, 1, 'Мусор', TRUE, 'unknown')"
            )
    finally:
        connection.rollback()
        connection.close()
