-- Migration 012: модуль loophole — RBAC (роли Authentik и override ролей пользователей).
-- Идемпотентно, диалект Greenplum 6 (БЕЗ PRIMARY KEY / UNIQUE-конструкций).

-- ── Маппинг групп Authentik на роли ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_role_mapping (
    group_name  TEXT,
    role_name   TEXT CHECK (role_name IN ('admin', 'cko', 'parser_dev', 'user')),
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lrm_group ON loophole_role_mapping(group_name);

-- ── Override ролей пользователей ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_user_role (
    user_id     TEXT,
    role_name   TEXT CHECK (role_name IN ('admin', 'cko', 'parser_dev', 'user')),
    created_at  TIMESTAMPTZ DEFAULT now(),
    created_by  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_lur_user ON loophole_user_role(user_id);
