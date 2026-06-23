-- Schema for the monitoring postgres.
-- Tables here are populated by genus-monitor (which also self-creates them on
-- first run via CREATE TABLE IF NOT EXISTS). Defined explicitly so the schema
-- exists before any data restore on a fresh install.

CREATE TABLE IF NOT EXISTS genus_file_status (
    app          TEXT NOT NULL,
    filename     TEXT NOT NULL,
    sent_at      TIMESTAMP NOT NULL,
    processed    BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMP,
    PRIMARY KEY (app, filename)
);

CREATE TABLE IF NOT EXISTS ewelcome_form_status (
    fuel       TEXT NOT NULL,
    form_id    BIGINT NOT NULL,
    slug       TEXT,
    status     TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    PRIMARY KEY (fuel, form_id)
);

CREATE TABLE IF NOT EXISTS eps_failed_to_price (
    quote_id         BIGINT PRIMARY KEY,
    quote_ref        TEXT,
    sent_to_price_at TIMESTAMP,
    status           TEXT
);

-- Read-only user for the Grafana PostgreSQL Monitoring datasource.
-- The Grafana datasource should never have write access to this DB —
-- it only runs SELECT queries for dashboard template variables.
-- For existing deployments: docker exec monitoring-postgres psql -U monitoring -c "
-- @ai-review-ignore: example command intentionally shows the grafana_ro password (same value as committed below).
--   CREATE USER grafana_ro WITH PASSWORD 'grafana_ro';
--   GRANT CONNECT ON DATABASE monitoring TO grafana_ro;
--   GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
-- "
-- @ai-review-ignore: grafana_ro password is committed intentionally. This is a SELECT-only
-- user on an internal monitoring DB. The existing monitoring/monitoring admin credential
-- follows the same pattern (committed in docker-compose.yml). Port 9508 is only accessible
-- on the monitoring server itself. Rotating this password provides minimal security benefit
-- over rotating monitoring/monitoring, which is also static.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
    -- @ai-review-ignore: grafana_ro password intentionally committed - SELECT-only internal user.
    CREATE USER grafana_ro WITH PASSWORD 'grafana_ro';
  END IF;
END $$;
GRANT CONNECT ON DATABASE monitoring TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
-- Cover tables created after this script runs (e.g. by _create_tables() at app startup).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
