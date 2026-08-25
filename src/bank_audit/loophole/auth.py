"""RBAC-модуль loophole.

Единое место для:
- констант ролей и действий;
- статической policy dict[role, set[action]];
- конфигурации из env (AUTH_TRUSTED_HEADERS, DEV_AUTH_MOCK, USER_HEADER, …);
- dataclass UserPrincipal;
- resolve_role(user_id, groups, session) → str;
- FastAPI Depends: get_current_user, require_action, require_admin.

DB-обращения к loophole_user_role / loophole_role_mapping делаются прямо здесь
через sqlalchemy.text() и константы из .db_schema — отдельный auth_repo.py
не создаётся (см. ТЗ).
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from .. import db
from .db_schema import T_ROLE_MAPPING, T_USER_ROLE

log = logging.getLogger(__name__)


# ── 1. Роли ──────────────────────────────────────────────────────────────────
ROLE_ADMIN = "admin"
ROLE_CKO = "cko"
ROLE_PARSER_DEV = "parser_dev"
ROLE_USER = "user"
VALID_ROLES: frozenset[str] = frozenset(
    {ROLE_ADMIN, ROLE_CKO, ROLE_PARSER_DEV, ROLE_USER}
)

# Приоритет ролей при резолве из group-mapping (от высшей к низшей).
_ROLE_PRIORITY: tuple[str, ...] = (
    ROLE_ADMIN,
    ROLE_CKO,
    ROLE_PARSER_DEV,
    ROLE_USER,
)


# ── 2. Действия ──────────────────────────────────────────────────────────────
ACT_READ_LOOPHOLES = "read_loopholes"
ACT_CHANGE_STATUS = "change_status"
ACT_CREATE_PARSER = "create_parser"
ACT_RUN_PARSER = "run_parser"
ACT_DELETE_PARSER = "delete_parser"
ACT_MANAGE_AUTH = "manage_auth"
VALID_ACTIONS: frozenset[str] = frozenset(
    {
        ACT_READ_LOOPHOLES,
        ACT_CHANGE_STATUS,
        ACT_CREATE_PARSER,
        ACT_RUN_PARSER,
        ACT_DELETE_PARSER,
        ACT_MANAGE_AUTH,
    }
)


# ── 3. Policy ────────────────────────────────────────────────────────────────
POLICY: dict[str, frozenset[str]] = {
    ROLE_USER: frozenset({ACT_READ_LOOPHOLES}),
    ROLE_PARSER_DEV: frozenset(
        {ACT_READ_LOOPHOLES, ACT_CREATE_PARSER, ACT_RUN_PARSER}
    ),
    ROLE_CKO: frozenset({ACT_READ_LOOPHOLES, ACT_CHANGE_STATUS}),
    ROLE_ADMIN: VALID_ACTIONS,
}


# ── 4. Конфигурация (env) ────────────────────────────────────────────────────
@dataclass(frozen=True)
class _AuthConfig:
    """Снимок переменных окружения AUTH_*/DEV_AUTH_*/DEFAULT_ROLE."""

    auth_trusted_headers: bool
    dev_auth_mock: bool
    user_header: str
    groups_header: str
    email_header: str
    default_role: str


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_auth_config() -> _AuthConfig:
    return _AuthConfig(
        auth_trusted_headers=_bool_env("AUTH_TRUSTED_HEADERS", False),
        dev_auth_mock=_bool_env("DEV_AUTH_MOCK", False),
        user_header=os.getenv("USER_HEADER", "X-Forwarded-User"),
        groups_header=os.getenv("GROUPS_HEADER", "X-Forwarded-Groups"),
        email_header=os.getenv("EMAIL_HEADER", "X-Forwarded-Email"),
        default_role=os.getenv("DEFAULT_ROLE", ROLE_USER),
    )


_CFG: _AuthConfig = _load_auth_config()


def auth_config() -> _AuthConfig:
    """Текущий снимок конфигурации (для диагностики/тестов)."""
    return _CFG


def reload_auth_config() -> _AuthConfig:
    """Перечитывает env-переменные. Полезно в тестах после monkeypatch."""
    global _CFG
    _CFG = _load_auth_config()
    return _CFG


# ── 5. UserPrincipal ────────────────────────────────────────────────────────
@dataclass
class UserPrincipal:
    """Идентичность текущего пользователя, видимая в Depends."""

    user_id: str
    email: str | None
    groups: list[str] = field(default_factory=list)
    role: str = ROLE_USER
    source: str = "header"  # "header" | "dev_mock"


# ── 6. resolve_role ──────────────────────────────────────────────────────────
@contextmanager
def _use_session(session) -> Iterator:
    """Использует переданную сессию или открывает новую через db.session()."""
    if session is not None:
        yield session
        return
    with db.session() as s:
        yield s


def _parse_csv(value: str | None) -> list[str]:
    """CSV → list непустых значений."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_role(user_id: str, groups: list[str], session) -> str:
    """sync. Определяет роль пользователя.

    Приоритет:
    1) override в loophole_user_role (per-user) — самая свежая запись;
    2) иначе если хотя бы одна группа есть в loophole_role_mapping —
       самая приоритетная роль (admin > cko > parser_dev > user);
    3) иначе default_role из конфига (валидируется).
    """
    cfg = _CFG

    # 1) per-user override
    if user_id:
        with _use_session(session) as s:
            row = s.execute(
                text(
                    f"SELECT role_name FROM {T_USER_ROLE} "
                    "WHERE user_id = :uid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"uid": user_id},
            ).scalar_one_or_none()
        if row and row in VALID_ROLES:
            return row

    # 2) маппинг групп → роли (берём самую приоритетную)
    if groups:
        with _use_session(session) as s:
            placeholders = ", ".join(f":g{i}" for i in range(len(groups)))
            params: dict[str, str] = {f"g{i}": g for i, g in enumerate(groups)}
            rows = (
                s.execute(
                    text(
                        f"SELECT DISTINCT role_name FROM {T_ROLE_MAPPING} "
                        f"WHERE group_name IN ({placeholders})"
                    ),
                    params,
                )
                .scalars()
                .all()
            )
        for role in _ROLE_PRIORITY:
            if role in rows:
                return role

    # 3) default
    return cfg.default_role if cfg.default_role in VALID_ROLES else ROLE_USER


def _deny(user_id: str, role: str, action: str) -> None:
    log.warning("[auth] denied user=%s role=%s action=%s", user_id, role, action)


# ── 7. FastAPI Depends ───────────────────────────────────────────────────────
def get_session():
    """Yield SQLAlchemy-сессию через db.session(). Переопределяется в тестах
    через app.dependency_overrides[get_session]."""
    with db.session() as s:
        yield s


async def get_current_user(
    request: Request,
    session=Depends(get_session),
) -> UserPrincipal:
    """Определяет текущего пользователя и кладёт в request.state.user.

    Правила:
    - AUTH_TRUSTED_HEADERS=False и DEV_AUTH_MOCK=False → 401 "auth not configured".
    - DEV_AUTH_MOCK=True → читаем DEV_AUTH_USER/GROUPS/ROLE (env), source="dev_mock".
    - иначе читаем USER_HEADER/GROUPS_HEADER/EMAIL_HEADER, source="header".
    - если задан DEV_AUTH_ROLE и он валиден — роль берётся напрямую
      (минуя resolve_role), что удобно для локальной разработки.
    """
    cached = getattr(request.state, "_loophole_user_principal", None)
    if cached is not None:
        return cached

    cfg = _CFG

    if not cfg.auth_trusted_headers and not cfg.dev_auth_mock:
        raise HTTPException(status_code=401, detail="auth not configured")

    user_id = ""
    email: str | None = None
    groups: list[str] = []
    source = "header"
    role: str | None = None

    if cfg.dev_auth_mock:
        user_id = os.getenv("DEV_AUTH_USER", "")
        email = os.getenv("DEV_AUTH_EMAIL") or None
        groups = _parse_csv(os.getenv("DEV_AUTH_GROUPS"))
        source = "dev_mock"
        direct_role = os.getenv("DEV_AUTH_ROLE", "")
        if direct_role in VALID_ROLES:
            role = direct_role
    elif cfg.auth_trusted_headers:
        user_id = request.headers.get(cfg.user_header, "") or ""
        email = request.headers.get(cfg.email_header) or None
        groups = _parse_csv(request.headers.get(cfg.groups_header))

    if role is None:
        role = resolve_role(user_id, groups, session)

    principal = UserPrincipal(
        user_id=user_id or "",
        email=email,
        groups=groups,
        role=role,
        source=source,
    )
    request.state._loophole_user_principal = principal
    request.state.user = principal
    return principal


def _check_action(principal: UserPrincipal, action: str) -> UserPrincipal:
    """Общая проверка policy: 401 если user_id пустой, 403 если роль не разрешает."""
    if not principal.user_id:
        _deny(principal.user_id or "(anonymous)", principal.role, action)
        raise HTTPException(status_code=401, detail="authentication required")
    allowed = POLICY.get(principal.role, frozenset())
    if action not in allowed:
        _deny(principal.user_id, principal.role, action)
        raise HTTPException(
            status_code=403,
            detail=f"role {principal.role!r} not allowed for action {action!r}",
        )
    return principal


def require_action(action: str):
    """Фабрика Depends. Кидает 401/403 согласно policy для указанного действия."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action: {action!r}")

    async def _dep(
        principal: UserPrincipal = Depends(get_current_user),
    ) -> UserPrincipal:
        return _check_action(principal, action)

    return _dep


async def require_admin(
    principal: UserPrincipal = Depends(get_current_user),
) -> UserPrincipal:
    """Обёртка над require_action(ACT_MANAGE_AUTH). Для CRUD маппинга и user-roles."""
    return _check_action(principal, ACT_MANAGE_AUTH)


__all__ = [
    # роли
    "ROLE_ADMIN",
    "ROLE_CKO",
    "ROLE_PARSER_DEV",
    "ROLE_USER",
    "VALID_ROLES",
    # действия
    "ACT_READ_LOOPHOLES",
    "ACT_CHANGE_STATUS",
    "ACT_CREATE_PARSER",
    "ACT_RUN_PARSER",
    "ACT_DELETE_PARSER",
    "ACT_MANAGE_AUTH",
    "VALID_ACTIONS",
    # policy / конфиг
    "POLICY",
    "auth_config",
    "reload_auth_config",
    # principal / resolve
    "UserPrincipal",
    "resolve_role",
    # FastAPI Depends
    "get_session",
    "get_current_user",
    "require_action",
    "require_admin",
]
