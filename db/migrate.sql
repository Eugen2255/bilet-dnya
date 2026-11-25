\set ON_ERROR_STOP on
BEGIN;

-- Serialize migration runs against the same database.
SELECT pg_advisory_xact_lock(76152401);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '001_core'
) AS apply_001_core \gset

\if :apply_001_core
    \ir migrations/001_core.sql
    INSERT INTO schema_migrations (version) VALUES ('001_core');
\endif

COMMIT;
