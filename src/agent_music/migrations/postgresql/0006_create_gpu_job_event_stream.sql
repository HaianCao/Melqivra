ALTER TABLE gpu_jobs
    ADD COLUMN IF NOT EXISTS next_event_sequence BIGINT NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'gpu_jobs_next_event_sequence_positive'
    ) THEN
        ALTER TABLE gpu_jobs
            ADD CONSTRAINT gpu_jobs_next_event_sequence_positive
            CHECK (next_event_sequence > 0);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS gpu_job_events (
    job_id TEXT NOT NULL REFERENCES gpu_jobs(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL CHECK (
        event_type ~ '^[a-z][a-z0-9_.-]{0,63}$'
    ),
    payload_json JSONB NOT NULL,
    job_state TEXT NOT NULL CHECK (
        job_state IN (
            'queued',
            'running',
            'succeeded',
            'failed',
            'cancelled',
            'dead_lettered'
        )
    ),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(job_id, sequence)
);

INSERT INTO gpu_job_events(
    job_id,
    sequence,
    event_type,
    payload_json,
    job_state,
    attempt_number,
    created_at
)
SELECT
    id,
    1,
    'job.snapshot',
    '{}'::jsonb,
    state,
    attempt_count,
    updated_at
FROM gpu_jobs
ON CONFLICT (job_id, sequence) DO NOTHING;

UPDATE gpu_jobs AS jobs
SET next_event_sequence = events.next_sequence
FROM (
    SELECT job_id, MAX(sequence) + 1 AS next_sequence
    FROM gpu_job_events
    GROUP BY job_id
) AS events
WHERE jobs.id = events.job_id
  AND jobs.next_event_sequence < events.next_sequence;
