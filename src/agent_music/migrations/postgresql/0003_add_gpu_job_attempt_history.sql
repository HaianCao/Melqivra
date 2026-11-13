ALTER TABLE gpu_jobs
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;

ALTER TABLE gpu_jobs
    DROP CONSTRAINT IF EXISTS gpu_jobs_state_check;

ALTER TABLE gpu_jobs
    ADD CONSTRAINT gpu_jobs_state_check CHECK (
        state IN (
            'queued',
            'running',
            'succeeded',
            'failed',
            'cancelled',
            'dead_lettered'
        )
    );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'gpu_jobs_max_attempts_positive'
    ) THEN
        ALTER TABLE gpu_jobs
            ADD CONSTRAINT gpu_jobs_max_attempts_positive CHECK (max_attempts > 0);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS gpu_job_attempts (
    job_id TEXT NOT NULL REFERENCES gpu_jobs(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    lease_token BIGINT NOT NULL UNIQUE,
    lease_owner TEXT NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,
    finished_at DOUBLE PRECISION,
    outcome TEXT NOT NULL CHECK (
        outcome IN (
            'running',
            'succeeded',
            'failed',
            'cancelled',
            'lease_expired',
            'interrupted'
        )
    ),
    error TEXT,
    PRIMARY KEY(job_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS ix_gpu_job_attempts_job_started
    ON gpu_job_attempts(job_id, started_at);

INSERT INTO gpu_job_attempts(
    job_id,
    attempt_number,
    lease_token,
    lease_owner,
    started_at,
    lease_expires_at,
    outcome
)
SELECT
    id,
    attempt_count,
    lease_token,
    COALESCE(lease_owner, 'legacy-worker'),
    COALESCE(started_at, created_at),
    COALESCE(lease_expires_at, updated_at),
    'running'
FROM gpu_jobs
WHERE state = 'running'
  AND attempt_count > 0
  AND lease_token IS NOT NULL
ON CONFLICT DO NOTHING;
