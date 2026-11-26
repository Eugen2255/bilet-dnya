CREATE TABLE daily_portions (
    course_id uuid NOT NULL,
    group_id uuid NOT NULL,
    member_id uuid NOT NULL,
    day date NOT NULL,
    daily_target integer NOT NULL CHECK (daily_target >= 0),
    assigned_count integer NOT NULL CHECK (assigned_count BETWEEN 0 AND 8),
    projected_missing integer NOT NULL CHECK (projected_missing >= 0),
    PRIMARY KEY (course_id, member_id, day),
    FOREIGN KEY (course_id, group_id) REFERENCES courses (id, group_id) ON DELETE CASCADE,
    FOREIGN KEY (member_id, group_id) REFERENCES memberships (id, group_id) ON DELETE CASCADE
);

CREATE TABLE repeats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id uuid NOT NULL,
    group_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    member_id uuid,
    due_day date NOT NULL,
    step smallint NOT NULL CHECK (step IN (1, 2)),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'issued', 'closed')),
    chain_id uuid NOT NULL DEFAULT gen_random_uuid(),
    origin_attempt_id uuid REFERENCES attempts ON DELETE CASCADE,
    UNIQUE (chain_id, step),
    FOREIGN KEY (course_id, group_id) REFERENCES courses (id, group_id) ON DELETE CASCADE,
    FOREIGN KEY (ticket_id, course_id) REFERENCES tickets (id, course_id) ON DELETE CASCADE,
    FOREIGN KEY (member_id, group_id) REFERENCES memberships (id, group_id) ON DELETE CASCADE,
    CONSTRAINT repeat_origin CHECK ((member_id IS NULL) = (origin_attempt_id IS NULL))
);

CREATE UNIQUE INDEX repeats_one_pending_personal
    ON repeats (ticket_id, member_id) WHERE state = 'pending' AND member_id IS NOT NULL;

CREATE UNIQUE INDEX repeats_one_pending_group
    ON repeats (ticket_id) WHERE state = 'pending' AND member_id IS NULL;

CREATE INDEX repeats_due_idx ON repeats (course_id, member_id, due_day) WHERE state = 'pending';
CREATE INDEX assignments_member_course_idx ON assignments (member_id, course_id, day);
