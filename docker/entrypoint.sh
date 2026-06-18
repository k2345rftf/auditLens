#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  AuditLens container entrypoint.
#    serve   (default) — exec uvicorn на 0.0.0.0 (миграции НЕ запускает)
#    migrate           — идемпотентно накатить migrations/*.sql + analytics/views.sql, выйти
#    <иное>            — exec переданной команды как есть
#
#  Миграции вынесены в отдельный режим (one-shot job), чтобы N реплик app не
#  гонели DDL параллельно. Авто-накат на старте — только если RUN_MIGRATIONS_ON_START=1.
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

MODE="${1:-serve}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/app/migrations}"
VIEWS_SQL="${VIEWS_SQL:-/app/src/bank_audit/analytics/views.sql}"
ENSURE_VECTOR_SQL="${ENSURE_VECTOR_SQL:-/app/migrations/ensure_vector.sql}"

# postgresql+psycopg://...  ->  postgresql://...  (psql не понимает суффикс +driver)
pg_dsn() {
  echo "${DATABASE_URL:?DATABASE_URL не задан}" | sed -E 's#^([a-zA-Z]+)\+[a-zA-Z0-9]+://#\1://#'
}

wait_for_db() {
  local dsn; dsn="$(pg_dsn)"
  local tries="${DB_WAIT_TRIES:-30}" i
  for ((i=1; i<=tries; i++)); do
    if psql "$dsn" -tAc 'SELECT 1' >/dev/null 2>&1; then
      echo "[entrypoint] БД доступна"; return 0
    fi
    echo "[entrypoint] жду БД ($i/$tries)…"; sleep 2
  done
  echo "[entrypoint] БД недоступна после $tries попыток" >&2; return 1
}

run_migrations() {
  local dsn; dsn="$(pg_dsn)"
  echo "[entrypoint] применяю миграции…"
  # Журнал применённых миграций (накатываем каждый файл максимум один раз).
  psql "$dsn" -v ON_ERROR_STOP=1 -q -c \
    "CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"
  local f base done_
  # Нумерованные миграции (NNN_*.sql) — каждая максимум один раз (через журнал).
  for f in "$MIGRATIONS_DIR"/[0-9]*.sql; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    done_="$(psql "$dsn" -tAc "SELECT 1 FROM schema_migrations WHERE filename='${base}'")"
    if [ "$done_" = "1" ]; then
      echo "[entrypoint]   skip $base (уже применена)"; continue
    fi
    echo "[entrypoint]   apply $base"
    psql "$dsn" -v ON_ERROR_STOP=1 -q -f "$f"
    psql "$dsn" -v ON_ERROR_STOP=1 -q -c \
      "INSERT INTO schema_migrations(filename) VALUES ('${base}') ON CONFLICT DO NOTHING;"
  done
  # ensure_vector.sql — НЕ журналируется, применяется КАЖДЫЙ раз (идемпотентно):
  # до-создаёт document_chunk.embedding + HNSW, как только суперюзер поставит pgvector.
  if [ -f "$ENSURE_VECTOR_SQL" ]; then
    echo "[entrypoint]   apply ensure_vector.sql"
    psql "$dsn" -v ON_ERROR_STOP=1 -q -f "$ENSURE_VECTOR_SQL"
  fi
  # Analytics-вью (CREATE OR REPLACE — идемпотентно; зависят от таблиц → после миграций).
  if [ -f "$VIEWS_SQL" ]; then
    echo "[entrypoint]   apply analytics/views.sql"
    psql "$dsn" -v ON_ERROR_STOP=1 -q -f "$VIEWS_SQL"
  fi
  echo "[entrypoint] миграции готовы"
}

case "$MODE" in
  migrate)
    wait_for_db
    run_migrations
    ;;
  serve)
    if [ "${RUN_MIGRATIONS_ON_START:-0}" = "1" ]; then
      wait_for_db || true
      run_migrations || echo "[entrypoint] миграции упали (продолжаю; проверьте права роли БД)" >&2
    fi
    echo "[entrypoint] uvicorn → ${APP_HOST}:${APP_PORT}"
    # exec → uvicorn получает PID 1 → корректный SIGTERM / graceful lifespan-shutdown.
    exec uvicorn bank_audit.web.app:app \
         --host "$APP_HOST" --port "$APP_PORT" \
         --proxy-headers --forwarded-allow-ips='*'
    ;;
  *)
    exec "$@"
    ;;
esac
