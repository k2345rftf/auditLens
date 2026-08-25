"""Интеграционные тесты RBAC: 4 роли × основные действия loophole.

Проверяется, что эндпоинты возвращают 401/403 согласно policy для разных ролей.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit.loophole.web import router, get_session
from bank_audit.loophole.auth import UserPrincipal, get_current_user

from .conftest import SCHEMA_SQL


@pytest.fixture
def app_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.connection.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


def _make_client(session, role: str, user_id: str = "test-user"):
    def ov_session():
        yield session

    def ov_user():
        return UserPrincipal(
            user_id=user_id, email=None, groups=[], role=role, source="test"
        )

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = ov_session
    app.dependency_overrides[get_current_user] = ov_user
    return TestClient(app)


# ── /records/{id}/status: только admin/cko ───────────────────────────────
def test_change_status_admin_allowed(app_session):
    # вставим запись
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.models import LoopholeRecord
    from bank_audit.hashing import sha256_text

    rec = LoopholeRecord(sha256=sha256_text("r1"), title="лазейка",
                         bank_slug="sberbank", status="new")
    rid = repo.insert_record(rec, session=app_session)

    c = _make_client(app_session, role="admin")
    r = c.post(f"/api/loophole/records/{rid}/status", json={"status": "in_review"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "in_review"


def test_change_status_cko_allowed(app_session):
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.models import LoopholeRecord
    from bank_audit.hashing import sha256_text

    rec = LoopholeRecord(sha256=sha256_text("cko1"), title="лазейка",
                         bank_slug="sberbank", status="new")
    rid = repo.insert_record(rec, session=app_session)

    c = _make_client(app_session, role="cko")
    r = c.post(f"/api/loophole/records/{rid}/status", json={"status": "fixed"})
    assert r.status_code == 200, r.text


def test_change_status_user_forbidden(app_session):
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.models import LoopholeRecord
    from bank_audit.hashing import sha256_text

    rec = LoopholeRecord(sha256=sha256_text("u1"), title="лазейка",
                         bank_slug="sberbank", status="new")
    rid = repo.insert_record(rec, session=app_session)

    c = _make_client(app_session, role="user")
    r = c.post(f"/api/loophole/records/{rid}/status", json={"status": "archived"})
    assert r.status_code == 403


def test_change_status_parser_dev_forbidden(app_session):
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.models import LoopholeRecord
    from bank_audit.hashing import sha256_text

    rec = LoopholeRecord(sha256=sha256_text("pd1"), title="лазейка",
                         bank_slug="sberbank", status="new")
    rid = repo.insert_record(rec, session=app_session)

    c = _make_client(app_session, role="parser_dev")
    r = c.post(f"/api/loophole/records/{rid}/status", json={"status": "fixed"})
    assert r.status_code == 403


def test_change_status_invalid_value(app_session):
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.models import LoopholeRecord
    from bank_audit.hashing import sha256_text

    rec = LoopholeRecord(sha256=sha256_text("iv1"), title="лазейка",
                         bank_slug="sberbank", status="new")
    rid = repo.insert_record(rec, session=app_session)

    c = _make_client(app_session, role="admin")
    r = c.post(f"/api/loophole/records/{rid}/status", json={"status": "bogus"})
    assert r.status_code == 400


def test_change_status_record_not_found(app_session):
    c = _make_client(app_session, role="admin")
    r = c.post("/api/loophole/records/99999/status", json={"status": "fixed"})
    assert r.status_code == 404


# ── Чтение: все роли могут просматривать ─────────────────────────────────
def test_read_loopholes_user_allowed(app_session):
    c = _make_client(app_session, role="user")
    r = c.get("/api/loophole/keywords")
    assert r.status_code == 200
    r = c.get("/api/loophole/records")
    assert r.status_code == 200


def test_read_loopholes_anon_denied(app_session):
    """anon (user_id пустой) → 401."""
    c = _make_client(app_session, role="user", user_id="")
    r = c.get("/api/loophole/keywords")
    assert r.status_code == 401


# ── Парсеры: create_parser и run_parser ─────────────────────────────────
def test_create_parser_admin_allowed(app_session, monkeypatch):
    from bank_audit.loophole.parsers import generator
    from bank_audit.loophole.parsers import registry as parser_registry
    from bank_audit.loophole.parsers import dedup as dedup_mod

    async def fake_gen(user_id, workspace_id, query, *, llm=None, session=None):
        return {
            "parser_id": 10, "validation_run_id": 1,
            "name": "fake_parser", "targets": [{"kind": "url", "value": "x"}],
        }

    # extract_targets возвращает URL → ключ
    def fake_extract(q):
        return ["https://example.com"]

    def fake_normalize(t):
        return "example.com"

    def fake_conflicts(keys, session=None):
        return []

    monkeypatch.setattr(generator, "extract_targets", fake_extract)
    monkeypatch.setattr(dedup_mod, "normalize_target", fake_normalize)
    monkeypatch.setattr(parser_registry, "find_conflicts", fake_conflicts)
    monkeypatch.setattr(generator, "generate_parser", fake_gen)

    # нужен workspace
    from bank_audit.loophole import workspace as ws_mod
    app_session.execute.__self__ if False else None  # noqa
    wid = ws_mod.create("test-user", name="ws", session=app_session)

    c = _make_client(app_session, role="admin")
    r = c.post("/api/loophole/parsers", json={"workspace_id": wid, "query": "url"})
    assert r.status_code == 200, r.text


def test_create_parser_user_forbidden(app_session):
    c = _make_client(app_session, role="user")
    r = c.post("/api/loophole/parsers", json={"workspace_id": 1, "query": "url"})
    assert r.status_code == 403


def test_create_parser_parser_dev_allowed(app_session, monkeypatch):
    """Разработчик парсеров может создавать, но НЕ может удалять/manage_auth."""
    from bank_audit.loophole.parsers import generator
    from bank_audit.loophole.parsers import registry as parser_registry
    from bank_audit.loophole.parsers import dedup as dedup_mod

    async def fake_gen(user_id, workspace_id, query, *, llm=None, session=None):
        return {
            "parser_id": 11, "validation_run_id": 1,
            "name": "fake_parser", "targets": [{"kind": "url", "value": "x"}],
        }

    monkeypatch.setattr(generator, "extract_targets", lambda q: ["https://example.com"])
    monkeypatch.setattr(dedup_mod, "normalize_target", lambda t: "example.com")
    monkeypatch.setattr(parser_registry, "find_conflicts", lambda keys, session=None: [])
    monkeypatch.setattr(generator, "generate_parser", fake_gen)

    from bank_audit.loophole import workspace as ws_mod
    wid = ws_mod.create("test-user", name="ws", session=app_session)

    c = _make_client(app_session, role="parser_dev")
    r = c.post("/api/loophole/parsers", json={"workspace_id": wid, "query": "url"})
    assert r.status_code == 200, r.text


def test_delete_parser_parser_dev_forbidden(app_session, monkeypatch):
    """parser_dev не может удалять парсеры (только admin)."""
    from bank_audit.loophole.parsers import runner as runner_mod
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(runner_mod, "_RUNNING", {})
    monkeypatch.setattr(parser_registry, "delete_parser", lambda pid, session=None: True)

    c = _make_client(app_session, role="parser_dev")
    r = c.delete("/api/loophole/parsers/9")
    assert r.status_code == 403


def test_delete_parser_admin_allowed(app_session, monkeypatch):
    from bank_audit.loophole.parsers import runner as runner_mod
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(runner_mod, "_RUNNING", {})
    monkeypatch.setattr(parser_registry, "delete_parser", lambda pid, session=None: True)

    c = _make_client(app_session, role="admin")
    r = c.delete("/api/loophole/parsers/9")
    assert r.status_code == 200, r.text


def test_run_parser_user_forbidden(app_session, monkeypatch):
    """user не может запускать парсеры."""
    from bank_audit.loophole import repository as repo
    monkeypatch.setattr(repo, "get_parser", lambda pid, session=None: {
        "parser_id": pid, "code_path": "/tmp/p.py", "workspace_id": 1,
    })
    c = _make_client(app_session, role="user")
    r = c.post("/api/loophole/parsers/9/run")
    assert r.status_code == 403


# ── /refine и /collect/run: только admin/cko ─────────────────────────────
def test_refine_user_forbidden(app_session):
    c = _make_client(app_session, role="user")
    r = c.post("/api/loophole/refine")
    assert r.status_code == 403


def test_refine_parser_dev_forbidden(app_session):
    c = _make_client(app_session, role="parser_dev")
    r = c.post("/api/loophole/refine")
    assert r.status_code == 403


def test_collect_run_user_forbidden(app_session):
    c = _make_client(app_session, role="user")
    r = c.post("/api/loophole/collect/run")
    assert r.status_code == 403


def test_collect_run_admin_allowed(app_session, monkeypatch):
    from bank_audit.loophole import collector as collector_mod
    async def fake_collect(session=None):
        return 0
    monkeypatch.setattr(collector_mod, "collect_once", fake_collect)
    c = _make_client(app_session, role="admin")
    r = c.post("/api/loophole/collect/run")
    assert r.status_code == 200, r.text


# ── /mark_verdict: только admin/cko ─────────────────────────────────────
def test_mark_verdict_user_forbidden(app_session):
    c = _make_client(app_session, role="user")
    r = c.post("/api/loophole/records/verdict",
               json={"record_ids": [1], "is_loophole": True})
    assert r.status_code == 403
