CREATE TABLE deletion_requests (
    user_id uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
    token uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    expires_at timestamptz NOT NULL
);

-- Course materials belong to the course. Remove the uploader's association on account deletion.
ALTER TABLE imports ALTER COLUMN member_id DROP NOT NULL;
ALTER TABLE imports DROP CONSTRAINT imports_member_id_group_id_fkey;
ALTER TABLE imports ADD FOREIGN KEY (member_id, group_id) REFERENCES memberships (id, group_id)
    ON DELETE SET NULL (member_id);

ALTER TABLE attempts ADD COLUMN acknowledged_at timestamptz, ADD COLUMN delivered_at timestamptz;
ALTER TABLE jobs ADD COLUMN finished_at timestamptz;
CREATE INDEX jobs_finished_at_idx ON jobs (finished_at) WHERE finished_at IS NOT NULL;

CREATE FUNCTION record_job_finish() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        NEW.finished_at := CASE WHEN NEW.state IN ('done', 'failed', 'delivery_unknown')
            THEN clock_timestamp() ELSE NULL END;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER jobs_record_finish BEFORE UPDATE OF state ON jobs
    FOR EACH ROW EXECUTE FUNCTION record_job_finish();

CREATE TABLE operation_errors (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL,
    provider text NOT NULL CHECK (provider IN ('model', 'telegram')),
    code text NOT NULL CHECK (code ~ '^[A-Z0-9_]{1,64}$'),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX operation_errors_occurred_at_idx ON operation_errors (occurred_at);
