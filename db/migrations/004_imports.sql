CREATE TABLE imports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id uuid NOT NULL,
    group_id uuid NOT NULL,
    member_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'processing', 'review', 'generating', 'ready', 'failed')),
    upload_refs jsonb,
    draft_questions jsonb,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    error jsonb,
    model text,
    prompt_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    uploads_expire_at timestamptz NOT NULL DEFAULT (now() + interval '24 hours'),
    UNIQUE (course_id, member_id, idempotency_key),
    UNIQUE (id, course_id),
    FOREIGN KEY (course_id, group_id) REFERENCES courses (id, group_id) ON DELETE CASCADE,
    FOREIGN KEY (member_id, group_id) REFERENCES memberships (id, group_id) ON DELETE CASCADE
);

CREATE INDEX import_upload_expiry_idx ON imports (uploads_expire_at) WHERE upload_refs IS NOT NULL;

CREATE TABLE sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id uuid NOT NULL,
    import_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    kind text NOT NULL CHECK (kind IN ('questions', 'material')),
    filename text NOT NULL,
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    pages jsonb NOT NULL CHECK (jsonb_typeof(pages) = 'array'),
    UNIQUE (import_id, ordinal),
    FOREIGN KEY (import_id, course_id) REFERENCES imports (id, course_id) ON DELETE CASCADE
);
