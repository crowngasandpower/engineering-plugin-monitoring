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
