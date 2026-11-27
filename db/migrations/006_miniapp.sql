ALTER TABLE study_groups ADD COLUMN title text;

CREATE TABLE web_sessions (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    csrf_hash text NOT NULL,
    expires_at timestamptz NOT NULL
);

CREATE INDEX web_sessions_expiry_idx ON web_sessions (expires_at);

CREATE TABLE course_activation_requests (
    course_id uuid NOT NULL REFERENCES courses ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    key text NOT NULL,
    acknowledge_load boolean NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (course_id, user_id, key)
);
