CREATE TABLE study_groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_chat_id bigint NOT NULL UNIQUE
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id bigint NOT NULL UNIQUE,
    dm_chat_id bigint
);

CREATE TABLE memberships (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id uuid NOT NULL REFERENCES study_groups ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('organizer', 'teacher', 'student')),
    alias text NOT NULL,
    rank_opt_in boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'active',
    UNIQUE (group_id, user_id),
    UNIQUE (id, group_id)
);

CREATE TABLE courses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id uuid NOT NULL REFERENCES study_groups ON DELETE CASCADE,
    title text NOT NULL CHECK (length(btrim(title)) > 0),
    exam_date date,
    timezone text,
    morning_at time,
    evening_at time,
    weekdays smallint[],
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'active', 'paused', 'closed')),
    UNIQUE (id, group_id),
    CONSTRAINT course_schedule_complete CHECK (
        state NOT IN ('active', 'paused') OR (
            exam_date IS NOT NULL AND timezone IS NOT NULL
            AND morning_at IS NOT NULL AND evening_at IS NOT NULL AND weekdays IS NOT NULL
        )
    ),
    CONSTRAINT course_schedule_hours CHECK (morning_at < evening_at),
    CONSTRAINT course_schedule_weekdays CHECK (
        CASE
            WHEN weekdays IS NULL THEN true
            WHEN array_ndims(weekdays) = 1 THEN cardinality(weekdays) BETWEEN 1 AND 7
                AND array_position(weekdays, NULL) IS NULL
                AND weekdays <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]
            ELSE false
        END
    )
);

CREATE UNIQUE INDEX courses_one_running_per_group
    ON courses (group_id) WHERE state IN ('active', 'paused');

CREATE TABLE tickets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id uuid NOT NULL REFERENCES courses ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    question text NOT NULL CHECK (length(btrim(question)) > 0 AND length(question) <= 500),
    state text NOT NULL DEFAULT 'draft',
    approved_rubric_id uuid,
    UNIQUE (course_id, ordinal),
    UNIQUE (id, course_id)
);

CREATE TABLE rubrics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id uuid NOT NULL REFERENCES tickets ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'approved', 'revoked')),
    answer text NOT NULL,
    source_status text NOT NULL,
    points jsonb NOT NULL CHECK (jsonb_typeof(points) = 'array'),
    author_id uuid REFERENCES users ON DELETE SET NULL,
    UNIQUE (ticket_id, version),
    UNIQUE (id, ticket_id)
);

ALTER TABLE tickets
    ADD CONSTRAINT ticket_approved_rubric_fk
    FOREIGN KEY (approved_rubric_id, id) REFERENCES rubrics (id, ticket_id)
    ON DELETE SET NULL (approved_rubric_id);

CREATE TABLE group_days (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id uuid NOT NULL REFERENCES courses ON DELETE CASCADE,
    day date NOT NULL,
    ticket_id uuid,
    state text NOT NULL,
    summary jsonb,
    UNIQUE (course_id, day),
    FOREIGN KEY (ticket_id, course_id) REFERENCES tickets (id, course_id)
        ON DELETE SET NULL (ticket_id)
);

CREATE TABLE assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id uuid NOT NULL,
    group_id uuid NOT NULL,
    member_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    day date NOT NULL,
    kind text NOT NULL,
    rubric_id uuid NOT NULL,
    state text NOT NULL,
    dm_message_id bigint,
    UNIQUE (member_id, ticket_id, day),
    FOREIGN KEY (course_id, group_id) REFERENCES courses (id, group_id) ON DELETE CASCADE,
    FOREIGN KEY (member_id, group_id) REFERENCES memberships (id, group_id) ON DELETE CASCADE,
    FOREIGN KEY (ticket_id, course_id) REFERENCES tickets (id, course_id) ON DELETE CASCADE,
    FOREIGN KEY (rubric_id, ticket_id) REFERENCES rubrics (id, ticket_id)
);

CREATE INDEX assignments_course_day_idx ON assignments (course_id, day);

CREATE TABLE attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id uuid NOT NULL UNIQUE REFERENCES assignments ON DELETE CASCADE,
    telegram_update_id bigint NOT NULL UNIQUE,
    body text CHECK (length(body) BETWEEN 1 AND 1000),
    submitted_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL,
    result jsonb,
    model text,
    prompt_version text,
    CONSTRAINT attempt_score_range CHECK (
        CASE
            WHEN result IS NULL THEN true
            WHEN jsonb_typeof(result->'score') = 'number' THEN
                (result->>'score')::numeric BETWEEN 0 AND 5
                AND (result->>'score')::numeric = trunc((result->>'score')::numeric)
            ELSE false
        END
    )
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key text NOT NULL UNIQUE,
    type text NOT NULL,
    payload jsonb NOT NULL,
    run_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'done', 'failed', 'delivery_unknown')),
    tries integer NOT NULL DEFAULT 0 CHECK (tries >= 0),
    lease_until timestamptz,
    result jsonb
);

CREATE INDEX jobs_queued_run_at_idx ON jobs (run_at, id) WHERE state = 'queued';
CREATE INDEX jobs_running_lease_idx ON jobs (lease_until) WHERE state = 'running';

CREATE TABLE telegram_updates (
    update_id bigint PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL DEFAULT 'received',
    payload jsonb
);

CREATE INDEX telegram_updates_received_at_idx ON telegram_updates (received_at);

CREATE TABLE action_tokens (
    token_hash text PRIMARY KEY,
    kind text NOT NULL,
    group_id uuid NOT NULL REFERENCES study_groups ON DELETE CASCADE,
    member_id uuid,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    FOREIGN KEY (member_id, group_id) REFERENCES memberships (id, group_id) ON DELETE CASCADE
);

CREATE FUNCTION validate_course_activation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    local_now timestamp;
    local_today date;
BEGIN
    IF NEW.state <> 'active' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.state IN ('active', 'paused') THEN
            RETURN NEW;
        END IF;
    END IF;

    IF NEW.exam_date IS NULL OR NEW.timezone IS NULL
        OR NEW.morning_at IS NULL OR NEW.evening_at IS NULL OR NEW.weekdays IS NULL THEN
        RAISE EXCEPTION 'Course activation requires exam date, time zone, and schedule'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'course_schedule_complete';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_timezone_names WHERE name = NEW.timezone) THEN
        RAISE EXCEPTION 'Unknown course time zone'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'course_activation_timezone';
    END IF;

    local_now := now() AT TIME ZONE NEW.timezone;
    local_today := local_now::date;
    IF NEW.exam_date <= local_today OR NOT EXISTS (
        SELECT FROM generate_series(0, 7) AS offset_days(n)
        WHERE local_today + n < NEW.exam_date
            AND (n > 0 OR local_now::time <= NEW.morning_at)
            AND extract(isodow FROM (local_today + n)) = ANY (NEW.weekdays)
    ) THEN
        RAISE EXCEPTION 'Course activation requires an upcoming study day before the exam'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'course_activation_study_day';
    END IF;

    IF NOT EXISTS (SELECT FROM tickets WHERE course_id = NEW.id) OR EXISTS (
        SELECT FROM tickets AS ticket
        LEFT JOIN rubrics AS rubric ON rubric.id = ticket.approved_rubric_id
        WHERE ticket.course_id = NEW.id AND rubric.state IS DISTINCT FROM 'approved'
    ) THEN
        RAISE EXCEPTION 'Course activation requires a ticket set with approved rubrics'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'course_activation_rubrics';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER courses_validate_activation
    BEFORE INSERT OR UPDATE OF state ON courses
    FOR EACH ROW EXECUTE FUNCTION validate_course_activation();
