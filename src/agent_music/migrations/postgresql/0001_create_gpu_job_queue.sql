CREATE SEQUENCE IF NOT EXISTS gpu_fencing_token_seq AS BIGINT START WITH 1;

CREATE TABLE IF NOT EXISTS gpu_jobs (
    enqueue_order BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    result_json JSONB,
    error TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    lease_token BIGINT,
    idempotency_key TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_gpu_jobs_user_idempotency
    ON gpu_jobs(user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_gpu_jobs_state_created
    ON gpu_jobs(state, created_at, enqueue_order);
