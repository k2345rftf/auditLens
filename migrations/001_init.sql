-- Bank audit platform :: core schema
-- Идемпотентно (CREATE ... IF NOT EXISTS / guarded CREATE TYPE) — безопасно
-- применять повторно (контейнерный entrypoint, managed-PG Облака УВА).

-- pgcrypto ставит суперюзер (в managed-PG роль приложения не суперюзер).
-- Не валим миграцию, если прав нет — расширение должен поставить ОАИТ заранее.
DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS pgcrypto;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'pgcrypto: нет прав — должен установить суперюзер ОАИТ (CREATE EXTENSION pgcrypto)';
END $$;

CREATE TABLE IF NOT EXISTS bank (
  bank_id      BIGSERIAL PRIMARY KEY,
  slug         TEXT NOT NULL UNIQUE,           -- 'sberbank', 'vtb'
  name         TEXT NOT NULL,
  aliases      TEXT[] NOT NULL DEFAULT '{}',
  is_sber      BOOLEAN NOT NULL DEFAULT FALSE,
  license_no   TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bank_aliases_gin ON bank USING gin (aliases);

-- Категории продуктов как enum (расширяемо). Guarded — повторный прогон не падает.
DO $$ BEGIN
  CREATE TYPE product_category AS ENUM (
    'deposit', 'credit', 'card_debit', 'card_credit',
    'mortgage', 'auto_loan', 'metals', 'investment', 'insurance', 'other'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS source_page (
  source_page_id BIGSERIAL PRIMARY KEY,
  source         TEXT NOT NULL,                -- 'sravni_aggregator' | 'banki_reviews' ...
  url_norm       TEXT NOT NULL,
  category       product_category,
  filter_context JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, url_norm)
);

CREATE TABLE IF NOT EXISTS extraction_run (
  run_id         BIGSERIAL PRIMARY KEY,
  source         TEXT NOT NULL,
  target_name    TEXT NOT NULL,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  status         TEXT NOT NULL DEFAULT 'running', -- running|ok|partial|failed
  items_seen     INT NOT NULL DEFAULT 0,
  items_written  INT NOT NULL DEFAULT 0,
  error          TEXT,
  openclaw_job   TEXT,
  meta           JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS extraction_run_started ON extraction_run(started_at DESC);

CREATE TABLE IF NOT EXISTS source_snapshot (
  snapshot_id    BIGSERIAL PRIMARY KEY,
  source_page_id BIGINT NOT NULL REFERENCES source_page(source_page_id) ON DELETE CASCADE,
  run_id         BIGINT REFERENCES extraction_run(run_id),
  fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  http_status    INT,
  content_sha256 TEXT NOT NULL,
  storage_path   TEXT NOT NULL,                -- путь в workspace/raw
  bytes          INT,
  UNIQUE (source_page_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS snapshot_page_time ON source_snapshot(source_page_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS product_offer (
  offer_id       BIGSERIAL PRIMARY KEY,
  bank_id        BIGINT NOT NULL REFERENCES bank(bank_id),
  category       product_category NOT NULL,
  external_id    TEXT NOT NULL,                -- ID источника или sha256(ключевых полей)
  primary_source TEXT NOT NULL,
  title          TEXT NOT NULL,
  url            TEXT,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (bank_id, category, external_id)
);

-- SCD2: историзация условий предложения
CREATE TABLE IF NOT EXISTS product_terms (
  terms_id       BIGSERIAL PRIMARY KEY,
  offer_id       BIGINT NOT NULL REFERENCES product_offer(offer_id) ON DELETE CASCADE,
  valid_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to       TIMESTAMPTZ,                  -- NULL = текущая версия
  rate_pct       NUMERIC(7,4),                 -- ставка/доходность
  rate_kind      TEXT,                         -- 'effective'|'nominal'|'max'|'min'
  currency       TEXT NOT NULL DEFAULT 'RUB',
  amount_min     NUMERIC(18,2),
  amount_max     NUMERIC(18,2),
  term_months_min INT,
  term_months_max INT,
  fee_open       NUMERIC(18,2),
  fee_service    NUMERIC(18,2),
  early_withdraw BOOLEAN,
  capitalization BOOLEAN,
  replenishable  BOOLEAN,
  conditions     TEXT,
  raw            JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_snapshot_id BIGINT REFERENCES source_snapshot(snapshot_id),
  filter_context_id  BIGINT,                   -- ссылка на тот же source_page (если из агрегатора)
  digest         TEXT NOT NULL                 -- sha256 нормализованных полей для сравнения
);
CREATE INDEX IF NOT EXISTS terms_offer_current ON product_terms(offer_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS terms_offer_history ON product_terms(offer_id, valid_from DESC);

-- search_result_set связывает один запуск агрегатора с найденными офферами
CREATE TABLE IF NOT EXISTS search_result_set (
  set_id         BIGSERIAL PRIMARY KEY,
  run_id         BIGINT NOT NULL REFERENCES extraction_run(run_id),
  source_page_id BIGINT NOT NULL REFERENCES source_page(source_page_id),
  position       INT NOT NULL,
  offer_id       BIGINT NOT NULL REFERENCES product_offer(offer_id),
  UNIQUE (run_id, source_page_id, offer_id)
);

-- Лог изменений: что именно поменялось между двумя версиями terms
CREATE TABLE IF NOT EXISTS change_history (
  change_id      BIGSERIAL PRIMARY KEY,
  offer_id       BIGINT NOT NULL REFERENCES product_offer(offer_id),
  prev_terms_id  BIGINT REFERENCES product_terms(terms_id),
  new_terms_id   BIGINT NOT NULL REFERENCES product_terms(terms_id),
  changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  diff           JSONB NOT NULL                -- {field: {from, to}}
);
CREATE INDEX IF NOT EXISTS change_history_offer ON change_history(offer_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS quality_flag (
  flag_id        BIGSERIAL PRIMARY KEY,
  entity_type    TEXT NOT NULL,                -- 'offer'|'terms'|'review'|'snapshot'
  entity_id      BIGINT NOT NULL,
  severity       TEXT NOT NULL,                -- 'info'|'warn'|'error'
  code           TEXT NOT NULL,                -- 'STALE'|'RATE_JUMP'|'MISSING_FIELD'...
  detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quality_flag_entity ON quality_flag(entity_type, entity_id);
