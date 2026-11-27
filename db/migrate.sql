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

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '005_rubrics'
) AS apply_005_rubrics \gset

\if :apply_005_rubrics
    \ir migrations/005_rubrics.sql
    INSERT INTO schema_migrations (version) VALUES ('005_rubrics');
\endif

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '006_miniapp'
) AS apply_006_miniapp \gset

\if :apply_006_miniapp
    \ir migrations/006_miniapp.sql
    INSERT INTO schema_migrations (version) VALUES ('006_miniapp');
\endif

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '007_reviews'
) AS apply_007_reviews \gset

\if :apply_007_reviews
    \ir migrations/007_reviews.sql
    INSERT INTO schema_migrations (version) VALUES ('007_reviews');
\endif

SELECT NOT EXISTS (
    SELECT FROM schema_migrations WHERE version = '008_operations'
) AS apply_008_operations \gset

\if :apply_008_operations
    \ir migrations/008_operations.sql
    INSERT INTO schema_migrations (version) VALUES ('008_operations');
\endif

COMMIT;
