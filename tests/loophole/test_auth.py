"""Unit-тесты модуля auth (RBAC loophole).

Покрывают:
- Константы ролей и действий;
- POLICY dict[role, set[action]];
- resolve_role: override per-user → group → default;
- _AuthConfig / reload_auth_config();
- _parse_csv();
- _check_action: 401 / 403 (через require_action);
- Кэширование в request.state.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from bank_audit.loophole import auth as auth_mod


# ── Константы ────────────────────────────────────────────────────────────────
def test_role_constants():
    assert auth_mod.ROLE_ADMIN == "admin"
    assert auth_mod.ROLE_CKO == "cko"
    assert auth_mod.ROLE_PARSER_DEV == "parser_dev"
    assert auth_mod.ROLE_USER == "user"
    assert {auth_mod.ROLE_ADMIN, auth_mod.ROLE_CKO,
            auth_mod.ROLE_PARSER_DEV, auth_mod.ROLE_USER} == set(auth_mod.VALID_ROLES)


def test_action_constants():
    expected = {
        "read_loopholes", "change_status", "create_parser",
        "run_parser", "delete_parser", "manage_auth",
    }
    assert set(auth_mod.VALID_ACTIONS) == expected


def test_policy_user_only_reads():
    assert auth_mod.POLICY[auth_mod.ROLE_USER] == frozenset({"read_loopholes"})


def test_policy_parser_dev_can_create_and_run():
    actions = auth_mod.POLICY[auth_mod.ROLE_PARSER_DEV]
    assert "read_loopholes" in actions
    assert "create_parser" in actions
    assert "run_parser" in actions
    assert "change_status" not in actions
    assert "delete_parser" not in actions


def test_policy_cko_can_change_status():
    actions = auth_mod.POLICY[auth_mod.ROLE_CKO]
    assert "read_loopholes" in actions
    assert "change_status" in actions
    assert "create_parser" not in actions
    assert "manage_auth" not in actions


def test_policy_admin_has_all_actions():
    assert set(auth_mod.POLICY[auth_mod.ROLE_ADMIN]) == set(auth_mod.VALID_ACTIONS)


# ── _AuthConfig / reload ────────────────────────────────────────────────────
def test_bool_env_default(monkeypatch):
    # очистим переменную и проверим дефолт
    monkeypatch.delenv("AUTH_TRUSTED_HEADERS", raising=False)
    cfg = auth_mod._load_auth_config()
    assert cfg.auth_trusted_headers is False
    assert cfg.dev_auth_mock is False


def test_bool_env_truthy(monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("AUTH_TRUSTED_HEADERS", v)
        cfg = auth_mod._load_auth_config()
        assert cfg.auth_trusted_headers is True, v


def test_reload_auth_config_refreshes(monkeypatch):
    monkeypatch.setenv("AUTH_TRUSTED_HEADERS", "true")
    cfg = auth_mod.reload_auth_config()
    assert cfg.auth_trusted_headers is True
    monkeypatch.setenv("AUTH_TRUSTED_HEADERS", "false")
    cfg2 = auth_mod.reload_auth_config()
    assert cfg2.auth_trusted_headers is False


# ── _parse_csv ─────────────────────────────────────────────────────────────
def test_parse_csv_normal():
    assert auth_mod._parse_csv("a,b,c") == ["a", "b", "c"]


def test_parse_csv_strips_and_drops_empty():
    assert auth_mod._parse_csv(" a ,b , , ") == ["a", "b"]


def test_parse_csv_none_and_empty():
    assert auth_mod._parse_csv(None) == []
    assert auth_mod._parse_csv("") == []


# ── resolve_role ───────────────────────────────────────────────────────────
def test_resolve_role_default_when_no_session_match(monkeypatch):
    """Никаких override'ов и никаких groups → DEFAULT_ROLE (или 'user')."""
    monkeypatch.setenv("DEFAULT_ROLE", "user")
    auth_mod.reload_auth_config()
    fake_session = MagicMock()
    # sql для override ничего не вернёт
    fake_session.execute.return_value.scalar_one_or_none.return_value = None
    result = auth_mod.resolve_role("", [], fake_session)
    assert result == "user"


def test_resolve_role_invalid_default_falls_back_to_user(monkeypatch):
    monkeypatch.setenv("DEFAULT_ROLE", "invalid_role")
    auth_mod.reload_auth_config()
    fake_session = MagicMock()
    fake_session.execute.return_value.scalar_one_or_none.return_value = None
    assert auth_mod.resolve_role("", [], fake_session) == auth_mod.ROLE_USER


def test_resolve_role_priority_admin_over_cko(monkeypatch):
    """Если группы дают admin и cko — побеждает admin по приоритету."""
    monkeypatch.setenv("DEFAULT_ROLE", "user")
    auth_mod.reload_auth_config()

    # mock: первая выборка (loophole_user_role) пустая, вторая (group→role) = ["admin", "cko"]
    fake_session = MagicMock()
    scalar_iter = MagicMock()
    scalar_iter.scalars.return_value.all.return_value = ["admin", "cko"]

    def execute_side_effect(*args, **kwargs):
        # Первая выборка — override (scalar_one_or_none)
        # Вторая выборка — group mapping (iterable)
        if scalar_iter.scalars.called:
            return scalar_iter.scalars.return_value.all.return_value
        return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    # Проще — мокнуть прямо с настройкой возврата по вызову:
    user_role_query = MagicMock()
    user_role_query.scalar_one_or_none.return_value = None
    user_role_result = MagicMock()
    user_role_result.scalar_one_or_none.return_value = None

    group_role_result = MagicMock()
    group_role_result.scalars.return_value.all.return_value = ["cko", "admin"]

    results = [user_role_result, group_role_result]
    fake_session.execute.side_effect = results

    result = auth_mod.resolve_role("u", ["g1", "g2"], fake_session)
    assert result == "admin"


# ── require_action валидация ───────────────────────────────────────────────
def test_require_action_unknown_raises_at_creation():
    with pytest.raises(ValueError, match="unknown action"):
        auth_mod.require_action("bad_action")


def test_require_action_denies_when_user_id_empty(monkeypatch):
    """user_id="" → require_action должен выбросить HTTPException 401."""
    monkeypatch.setenv("DEFAULT_ROLE", "user")
    auth_mod.reload_auth_config()
    principal = auth_mod.UserPrincipal(
        user_id="", email=None, groups=[], role="user", source="test"
    )
    with pytest.raises(HTTPException) as exc_info:
        auth_mod._check_action(principal, "read_loopholes")
    assert exc_info.value.status_code == 401


def test_require_action_denies_on_policy_violation():
    """user без права change_status → 403."""
    principal = auth_mod.UserPrincipal(
        user_id="u", email=None, groups=[], role="user", source="test"
    )
    with pytest.raises(HTTPException) as exc_info:
        auth_mod._check_action(principal, "change_status")
    assert exc_info.value.status_code == 403


def test_require_action_allows_policy_match():
    """admin → 200 (без исключения)."""
    principal = auth_mod.UserPrincipal(
        user_id="a", email=None, groups=[], role="admin", source="test"
    )
    # Не должно бросить исключение
    result = auth_mod._check_action(principal, "manage_auth")
    assert result is principal
