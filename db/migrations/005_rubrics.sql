ALTER TABLE imports
    ADD COLUMN confirmed_by uuid,
    ADD COLUMN confirm_key text,
    ADD COLUMN confirm_hash text,
    ADD FOREIGN KEY (confirmed_by, group_id) REFERENCES memberships (id, group_id)
        ON DELETE SET NULL (confirmed_by);

CREATE UNIQUE INDEX imports_one_generating_per_course
    ON imports (course_id) WHERE state = 'generating';

ALTER TABLE tickets
    ADD COLUMN import_id uuid,
    ADD FOREIGN KEY (import_id, course_id) REFERENCES imports (id, course_id) ON DELETE SET NULL (import_id);

CREATE INDEX tickets_import_idx ON tickets (import_id);

ALTER TABLE rubrics
    ADD COLUMN model text,
    ADD COLUMN prompt_version text;

CREATE FUNCTION preserve_published_rubric() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.ticket_id, NEW.version, NEW.answer, NEW.source_status, NEW.points, NEW.model, NEW.prompt_version)
        IS DISTINCT FROM
       (OLD.id, OLD.ticket_id, OLD.version, OLD.answer, OLD.source_status, OLD.points, OLD.model, OLD.prompt_version)
        OR NEW.state = 'draft' OR (OLD.state = 'revoked' AND NEW.state <> 'revoked')
    THEN
        RAISE EXCEPTION 'Published rubric contents are immutable'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'published_rubric_immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER rubrics_preserve_published
    BEFORE UPDATE ON rubrics
    FOR EACH ROW WHEN (OLD.state IN ('approved', 'revoked'))
    EXECUTE FUNCTION preserve_published_rubric();

ALTER TABLE sources ADD UNIQUE (id, course_id);

CREATE TABLE chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL,
    course_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    locator jsonb NOT NULL CHECK (jsonb_typeof(locator) = 'object'),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 1200),
    search_terms tsvector GENERATED ALWAYS AS (to_tsvector('russian'::regconfig, body)) STORED,
    UNIQUE (source_id, ordinal),
    FOREIGN KEY (source_id, course_id) REFERENCES sources (id, course_id) ON DELETE CASCADE
);

CREATE INDEX chunks_search_idx ON chunks USING gin (search_terms);
CREATE INDEX chunks_course_idx ON chunks (course_id);
