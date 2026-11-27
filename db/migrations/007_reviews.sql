CREATE TABLE appeals (
    attempt_id uuid PRIMARY KEY REFERENCES attempts ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'succeeded', 'failed')),
    result jsonb,
    model text,
    prompt_version text,
    reviewed_at timestamptz,
    CHECK ((state = 'succeeded') = (result IS NOT NULL)),
    CHECK ((state = 'queued') = (reviewed_at IS NULL)),
    CHECK (result IS NULL OR COALESCE(
        jsonb_typeof(result->'score') = 'number' AND (result->>'score')::numeric IN (0, 1, 2, 3, 4, 5), false
    ))
);

CREATE TABLE rubric_reports (
    rubric_id uuid NOT NULL REFERENCES rubrics ON DELETE CASCADE,
    member_id uuid NOT NULL REFERENCES memberships ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (rubric_id, member_id)
);

CREATE INDEX assignments_rubric_open_idx ON assignments (rubric_id) WHERE state = 'open';

CREATE TABLE personal_repeat_evidence (
    first_repeat_id uuid NOT NULL REFERENCES repeats ON DELETE CASCADE,
    attempt_id uuid NOT NULL REFERENCES attempts ON DELETE CASCADE,
    PRIMARY KEY (first_repeat_id, attempt_id)
);

-- Preserve earlier weak grades supporting chains that can still be changed.
INSERT INTO personal_repeat_evidence (first_repeat_id, attempt_id)
SELECT first.id, p.id
FROM repeats AS first
JOIN attempts AS origin ON origin.id = first.origin_attempt_id
JOIN assignments AS a ON a.member_id = first.member_id AND a.ticket_id = first.ticket_id
JOIN attempts AS p ON p.assignment_id = a.id
WHERE first.step = 1 AND p.status = 'graded' AND (p.result->>'score')::int < 3
    AND (p.id = origin.id OR p.graded_at >= origin.graded_at)
    AND EXISTS (SELECT 1 FROM repeats AS next WHERE next.chain_id = first.chain_id AND next.state = 'pending');
