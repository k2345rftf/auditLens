"""REST-CRUD для admin-управления RBAC loophole.

Эндпоинты:
- GET    /api/loophole/auth/role-mappings
- POST   /api/loophole/auth/role-mappings
- DELETE /api/loophole/auth/role-mappings/{group_name}
- GET    /api/loophole/auth/user-roles
- POST   /api/loophole/auth/user-roles
- DELETE /api/loophole/auth/user-roles/{user_id}
- GET    /api/loophole/auth/me

Префикс /api/loophole (монтируется в web/app.py). Авторизация — через Depends
из .auth (require_admin / get_current_user). Все мутирующие эндпоинты пишут
в loophole_action_log через .logging_audit.log_action. Прямой SQL через
sqlalchemy.text(), без ORM, как и в остальном модуле loophole.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from . import logging_audit
from .auth import (
    POLICY,
    UserPrincipal,
    VALID_ROLES,
    get_current_user,
    get_session,
    require_admin,
)
from .db_schema import T_ROLE_MAPPING, T_USER_ROLE

router = APIRouter(tags=["loophole-auth-admin"])


# ── Pydantic-модели ──────────────────────────────────────────────────────────
class RoleMappingUpsert(BaseModel):
    group_name: str
    role_name: str


class RoleMappingOut(BaseModel):
    group_name: str
    role_name: str
    created_at: datetime


class UserRoleUpsert(BaseModel):
    user_id: str
    role_name: str
    note: Optional[str] = None


class UserRoleOut(BaseModel):
    user_id: str
    role_name: str
    created_at: datetime
    created_by: Optional[str]
    note: Optional[str]


# ── Вспомогательные функции ──────────────────────────────────────────────────
def _validate_role(role_name: str) -> None:
    """400 если role_name не входит в VALID_ROLES."""
    if role_name not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown role {role_name!r}; expected one of {sorted(VALID_ROLES)}",
        )


def _row_to_role_mapping(row: Any) -> RoleMappingOut:
    """Преобразует строку БД в RoleMappingOut (coerce created_at)."""
    created_at = row.created_at
    if not isinstance(created_at, datetime):
        created_at = datetime.fromisoformat(str(created_at))
    return RoleMappingOut(
        group_name=row.group_name,
        role_name=row.role_name,
        created_at=created_at,
    )


def _row_to_user_role(row: Any) -> UserRoleOut:
    """Преобразует строку БД в UserRoleOut (coerce created_at)."""
    created_at = row.created_at
    if not isinstance(created_at, datetime):
        created_at = datetime.fromisoformat(str(created_at))
    return UserRoleOut(
        user_id=row.user_id,
        role_name=row.role_name,
        created_at=created_at,
        created_by=row.created_by,
        note=row.note,
    )


# ── Эндпоинты: role-mapping ──────────────────────────────────────────────────
@router.get(
    "/auth/role-mappings",
    response_model=list[RoleMappingOut],
)
def list_role_mappings(
    user: UserPrincipal = Depends(require_admin),
    session=Depends(get_session),
):
    """Список маппинга группа → роль. Только admin."""
    rows = session.execute(
        text(
            f"SELECT group_name, role_name, created_at "
            f"FROM {T_ROLE_MAPPING} "
            f"ORDER BY group_name ASC"
        )
    ).all()
    return [_row_to_role_mapping(r) for r in rows]


@router.post(
    "/auth/role-mappings",
    response_model=RoleMappingOut,
)
def upsert_role_mapping(
    body: RoleMappingUpsert,
    user: UserPrincipal = Depends(require_admin),
    session=Depends(get_session),
):
    """Upsert маппинга группа → роль. Только admin."""
    _validate_role(body.role_name)

    session.execute(
        text(
            f"DELETE FROM {T_ROLE_MAPPING} WHERE group_name = :group_name"
        ),
        {"group_name": body.group_name},
    )
    session.execute(
        text(
            f"INSERT INTO {T_ROLE_MAPPING} (group_name, role_name) "
            f"VALUES (:group_name, :role_name)"
        ),
        {"group_name": body.group_name, "role_name": body.role_name},
    )
    session.commit()

    row = session.execute(
        text(
            f"SELECT group_name, role_name, created_at "
            f"FROM {T_ROLE_MAPPING} WHERE group_name = :group_name"
        ),
        {"group_name": body.group_name},
    ).one()

    logging_audit.log_action(
        user.user_id,
        "role_mapping_upsert",
        detail={"group_name": body.group_name, "role_name": body.role_name},
        session=session,
    )

    return _row_to_role_mapping(row)


@router.delete(
    "/auth/role-mappings/{group_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_role_mapping(
    group_name: str,
    user: UserPrincipal = Depends(require_admin),
    session=Depends(get_session),
):
    """Удалить маппинг группа → роль. 204 / 404. Только admin."""
    result = session.execute(
        text(f"DELETE FROM {T_ROLE_MAPPING} WHERE group_name = :group_name"),
        {"group_name": group_name},
    )
    session.commit()

    if getattr(result, "rowcount", 0) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"role mapping for group_name={group_name!r} not found",
        )

    logging_audit.log_action(
        user.user_id,
        "role_mapping_delete",
        detail={"group_name": group_name},
        session=session,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Эндпоинты: user-role override ────────────────────────────────────────────
@router.get(
    "/auth/user-roles",
    response_model=list[UserRoleOut],
)
def list_user_roles(
    user_id: Optional[str] = Query(default=None),
    user: UserPrincipal = Depends(require_admin),
    session=Depends(get_session),
):
    """Список override ролей. Только admin. Опциональный фильтр ?user_id=..."""
    if user_id is not None:
        rows = (
            session.execute(
                text(
                    f"SELECT user_id, role_name, created_at, created_by, note "
                    f"FROM {T_USER_ROLE} WHERE user_id = :uid "
                    f"ORDER BY user_id ASC, created_at DESC"
                ),
                {"uid": user_id},
            )
            .all()
        )
    else:
        rows = (
            session.execute(
                text(
                    f"SELECT user_id, role_name, created_at, created_by, note "
                    f"FROM {T_USER_ROLE} "
                    f"ORDER BY user_id ASC, created_at DESC"
                )
            )
            .all()
        )
    return [_row_to_user_role(r) for r in rows]


@router.post(
    "/auth/user-roles",
    response_model=UserRoleOut,
)
def upsert_user_role(
    body: UserRoleUpsert,
    user: UserPrincipal = Depends(require_admin),
    session=Depends(get_session),
):
    """Upsert override роли пользователя. Только admin."""
    _validate_role(body.role_name)

    session.execute(
        text(f"DELETE FROM {T_USER_ROLE} WHERE user_id = :uid"),
        {"uid": body.user_id},
    )
    session.execute(
        text(
            f"INSERT INTO {T_USER_ROLE} "
            f"(user_id, role_name, created_by, note) "
            f"VALUES (:uid, :role, :by, :note)"
        ),
        {
            "uid": body.user_id,
            "role": body.role_name,
            "by": user.user_id,
            "note": body.note,
        },
    )
    session.commit()

    row = session.execute(
        text(
            f"SELECT user_id, role_name, created_at, created_by, note "
            f"FROM {T_USER_ROLE} WHERE user_id = :uid "
            f"ORDER BY created_at DESC LIMIT 1"
        ),
        {"uid": body.user_id},
    ).one()

    logging_audit.log_action(
        user.user_id,
        "user_role_upsert",
        detail={
            "user_id": body.user_id,
            "role_name": body.role_name,
            "note": body.note,
        },
        session=session,
    )

    return _row_to_user_role(row)


@router.delete(
    "/auth/user-roles/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_user_role(
    user_id: str,
    admin: UserPrincipal = Depends(require_admin),
    session=Depends(get_session),
):
    """Удалить override роли пользователя. 204 / 404. Только admin."""
    result = session.execute(
        text(f"DELETE FROM {T_USER_ROLE} WHERE user_id = :uid"),
        {"uid": user_id},
    )
    session.commit()

    if getattr(result, "rowcount", 0) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"user_role override for user_id={user_id!r} not found",
        )

    logging_audit.log_action(
        admin.user_id,
        "user_role_delete",
        detail={"user_id": user_id},
        session=session,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Текущая идентичность (для UI) ───────────────────────────────────────────
def _policy_actions(role: str) -> list[str]:
    """Возвращает список действий роли из POLICY (отсортирован для UI)."""
    return sorted(POLICY.get(role, frozenset()))


@router.get("/auth/me")
def me(
    user: UserPrincipal = Depends(get_current_user),
) -> dict:
    """Текущая идентичность для UI. Не требует admin."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "groups": list(user.groups),
        "role": user.role,
        "actions": _policy_actions(user.role),
        "source": user.source,
    }
