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

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '002_worker'
) AS apply_002_worker \gset

\if :apply_002_worker
    \ir migrations/002_worker.sql
    INSERT INTO schema_migrations (version) VALUES ('002_worker');
\endif

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '003_practice'
) AS apply_003_practice \gset

\if :apply_003_practice
    \ir migrations/003_practice.sql
    INSERT INTO schema_migrations (version) VALUES ('003_practice');
\endif

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '004_imports'
) AS apply_004_imports \gset

\if :apply_004_imports
    \ir migrations/004_imports.sql
    INSERT INTO schema_migrations (version) VALUES ('004_imports');
\endif

COMMIT;
