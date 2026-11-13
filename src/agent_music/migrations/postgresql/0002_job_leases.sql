ALTER TABLE gpu_jobs
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

UPDATE gpu_jobs
SET state = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
    started_at = NULL,
    finished_at = CASE
        WHEN cancel_requested THEN EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)
        ELSE NULL
    END,
    lease_token = NULL,
    lease_owner = NULL,
    lease_expires_at = NULL,
    updated_at = EXTRACT(EPOCH FROM CURRENT_TIMESTAMP),
    error = CASE
        WHEN cancel_requested THEN 'Job cancelled during lease migration'
        ELSE 'Lease migration safely requeued interrupted work'
    END
WHERE state = 'running';

CREATE TABLE IF NOT EXISTS gpu_capacity_slots (
    slot_id SMALLINT PRIMARY KEY CHECK (slot_id = 1),
    active_job_id TEXT REFERENCES gpu_jobs(id),
    lease_token BIGINT,
    lease_owner TEXT,
    lease_expires_at DOUBLE PRECISION,
    CHECK (
        (
            active_job_id IS NULL
            AND lease_token IS NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
        OR
        (
            active_job_id IS NOT NULL
            AND lease_token IS NOT NULL
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    )
);

INSERT INTO gpu_capacity_slots(slot_id)
VALUES (1)
ON CONFLICT (slot_id) DO NOTHING;
