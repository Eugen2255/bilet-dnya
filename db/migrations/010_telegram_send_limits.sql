CREATE TABLE telegram_send_limits (
    -- Zero is the shared sendMessage limit; real chat IDs have their own rows.
    chat_id bigint PRIMARY KEY,
    next_send_at timestamptz NOT NULL DEFAULT '1970-01-01 00:00:00+00',
    blocked_at timestamptz,
    last_reachable_at timestamptz,
    CHECK (chat_id <> 0 OR blocked_at IS NULL)
);

INSERT INTO telegram_send_limits (chat_id) VALUES (0);
