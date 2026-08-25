"""Тесты CRUD-роутера auth_admin: маппинг групп→ролей и override ролей.

Используется in-memory SQLite через фикстуру `client` (см. test_web.py).
auth_admin-роутер монтируется как часть `web.router`.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
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


# ── /auth/me ─────────────────────────────────────────────────────────────
def test_auth_me_admin(app_session):
    c = _make_client(app_session, role="admin")
    r = c.get("/api/loophole/auth/me")
    assert r.status_code == 200
    data = r.json()
    assert data["user_id"] == "test-user"
    assert data["role"] == "admin"
    assert "manage_auth" in data["actions"]
    assert "read_loopholes" in data["actions"]


def test_auth_me_user(app_session):
    c = _make_client(app_session, role="user")
    r = c.get("/api/loophole/auth/me")
    assert r.status_code == 200
    data = r.json()
    assert data["role"] == "user"
    assert data["actions"] == ["read_loopholes"]


# ── /auth/role-mappings: только admin ─────────────────────────────────────
def test_role_mappings_list_admin(app_session):
    c = _make_client(app_session, role="admin")
    r = c.get("/api/loophole/auth/role-mappings")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_role_mappings_list_user_forbidden(app_session):
    c = _make_client(app_session, role="user")
    r = c.get("/api/loophole/auth/role-mappings")
    assert r.status_code == 403


def test_role_mappings_upsert_and_list(app_session):
    c = _make_client(app_session, role="admin")

    # POST /auth/role-mappings
    r = c.post("/api/loophole/auth/role-mappings",
               json={"group_name": "uabora/parsers", "role_name": "parser_dev"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_name"] == "uabora/parsers"
    assert body["role_name"] == "parser_dev"

    # GET
    r = c.get("/api/loophole/auth/role-mappings")
    assert r.status_code == 200
    items = r.json()
    assert any(m["group_name"] == "uabora/parsers" for m in items)
    assert any(m["role_name"] == "parser_dev" for m in items)


def test_role_mapping_invalid_role_rejected(app_session):
    c = _make_client(app_session, role="admin")
    r = c.post("/api/loophole/auth/role-mappings",
               json={"group_name": "x", "role_name": "bogus_role"})
    assert r.status_code == 400


def test_role_mapping_delete(app_session):
    c = _make_client(app_session, role="admin")
    c.post("/api/loophole/auth/role-mappings",
           json={"group_name": "g-delete", "role_name": "cko"})
    r = c.delete("/api/loophole/auth/role-mappings/g-delete")
    assert r.status_code == 204
    r = c.delete("/api/loophole/auth/role-mappings/g-delete")
    assert r.status_code == 404


# ── /auth/user-roles: только admin ────────────────────────────────────────
def test_user_roles_upsert_and_list(app_session):
    c = _make_client(app_session, role="admin", user_id="admin-user")

    r = c.post("/api/loophole/auth/user-roles",
               json={"user_id": "ivanov", "role_name": "parser_dev", "note": "test"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "ivanov"
    assert body["role_name"] == "parser_dev"
    assert body["created_by"] == "admin-user"
    assert body["note"] == "test"

    r = c.get("/api/loophole/auth/user-roles")
    assert r.status_code == 200
    items = r.json()
    assert any(u["user_id"] == "ivanov" for u in items)


def test_user_roles_filtered_list(app_session):
    c = _make_client(app_session, role="admin")
    c.post("/api/loophole/auth/user-roles",
           json={"user_id": "ivanov", "role_name": "cko"})
    c.post("/api/loophole/auth/user-roles",
           json={"user_id": "petrov", "role_name": "parser_dev"})
    r = c.get("/api/loophole/auth/user-roles", params={"user_id": "ivanov"})
    assert r.status_code == 200
    items = r.json()
    assert all(u["user_id"] == "ivanov" for u in items)


def test_user_roles_invalid_role_rejected(app_session):
    c = _make_client(app_session, role="admin")
    r = c.post("/api/loophole/auth/user-roles",
               json={"user_id": "u", "role_name": "bogus"})
    assert r.status_code == 400


def test_user_role_delete(app_session):
    c = _make_client(app_session, role="admin")
    c.post("/api/loophole/auth/user-roles",
           json={"user_id": "del-me", "role_name": "user"})
    r = c.delete("/api/loophole/auth/user-roles/del-me")
    assert r.status_code == 204
    r = c.delete("/api/loophole/auth/user-roles/del-me")
    assert r.status_code == 404


def test_user_roles_non_admin_forbidden(app_session):
    c = _make_client(app_session, role="cko")
    r = c.post("/api/loophole/auth/user-roles",
               json={"user_id": "u", "role_name": "admin"})
    assert r.status_code == 403
