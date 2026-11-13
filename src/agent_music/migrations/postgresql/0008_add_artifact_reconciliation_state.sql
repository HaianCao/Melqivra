ALTER TABLE artifact_batches
    ADD COLUMN state_changed_at DOUBLE PRECISION;

ALTER TABLE artifact_batches
    DISABLE TRIGGER controlled_artifact_batches_update;

UPDATE artifact_batches
SET state_changed_at = COALESCE(deleted_at, published_at, created_at);

ALTER TABLE artifact_batches
    ALTER COLUMN state_changed_at SET NOT NULL;

ALTER TABLE artifact_batches
    ADD CONSTRAINT artifact_batches_state_time_order CHECK (
        state_changed_at >= created_at
        AND (published_at IS NULL OR published_at >= created_at)
        AND (deleted_at IS NULL OR deleted_at >= created_at)
    );

ALTER TABLE artifacts
    ADD COLUMN state_changed_at DOUBLE PRECISION;

ALTER TABLE artifacts
    DISABLE TRIGGER controlled_artifacts_update;

UPDATE artifacts
SET state_changed_at = COALESCE(deleted_at, published_at, created_at);

ALTER TABLE artifacts
    ALTER COLUMN state_changed_at SET NOT NULL;

ALTER TABLE artifacts
    ADD CONSTRAINT artifacts_state_time_order CHECK (
        state_changed_at >= created_at
        AND (published_at IS NULL OR published_at >= created_at)
        AND (deleted_at IS NULL OR deleted_at >= created_at)
    );

DROP INDEX ix_artifact_batches_reconciliation;

CREATE INDEX ix_artifact_batches_reconciliation
    ON artifact_batches(state, state_changed_at, owner_id, project_id, batch_id);

CREATE OR REPLACE FUNCTION enforce_artifact_batch_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.owner_id,
        NEW.project_id,
        NEW.batch_id,
        NEW.contract_sha256,
        NEW.artifact_count,
        NEW.created_at
    ) IS DISTINCT FROM (
        OLD.owner_id,
        OLD.project_id,
        OLD.batch_id,
        OLD.contract_sha256,
        OLD.artifact_count,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION 'artifact batch contract is immutable';
    END IF;
    IF NEW.state = OLD.state THEN
        IF (
            NEW.state_changed_at,
            NEW.published_at,
            NEW.deleted_at
        ) IS DISTINCT FROM (
            OLD.state_changed_at,
            OLD.published_at,
            OLD.deleted_at
        ) THEN
            RAISE EXCEPTION 'artifact batch state timestamps are immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state IN ('published', 'corrupt', 'deleting')) OR
        (OLD.state = 'published' AND NEW.state IN ('corrupt', 'deleting')) OR
        (OLD.state = 'corrupt' AND NEW.state = 'deleting') OR
        (OLD.state = 'deleting' AND NEW.state = 'deleted')
    ) THEN
        RAISE EXCEPTION 'invalid artifact batch state transition';
    END IF;
    IF NEW.state_changed_at < OLD.state_changed_at THEN
        RAISE EXCEPTION 'artifact batch state time moved backwards';
    END IF;
    IF NEW.state = 'published' THEN
        IF NEW.published_at IS NULL OR OLD.published_at IS NOT NULL THEN
            RAISE EXCEPTION 'artifact batch publish timestamp is invalid';
        END IF;
    ELSIF NEW.published_at IS DISTINCT FROM OLD.published_at THEN
        RAISE EXCEPTION 'artifact batch publish timestamp changed unexpectedly';
    END IF;
    IF NEW.state = 'deleted' THEN
        IF NEW.deleted_at IS NULL OR OLD.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'artifact batch delete timestamp is invalid';
        END IF;
    ELSIF NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        RAISE EXCEPTION 'artifact batch delete timestamp changed unexpectedly';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_artifact_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.owner_id,
        NEW.project_id,
        NEW.batch_id,
        NEW.ordinal,
        NEW.id,
        NEW.object_key,
        NEW.media_type,
        NEW.size_bytes,
        NEW.sha256,
        NEW.source_revision_ids,
        NEW.created_at
    ) IS DISTINCT FROM (
        OLD.owner_id,
        OLD.project_id,
        OLD.batch_id,
        OLD.ordinal,
        OLD.id,
        OLD.object_key,
        OLD.media_type,
        OLD.size_bytes,
        OLD.sha256,
        OLD.source_revision_ids,
        OLD.created_at
    ) THEN
        RAISE EXCEPTION 'artifact contract is immutable';
    END IF;
    IF NEW.state = OLD.state THEN
        IF (
            NEW.state_changed_at,
            NEW.published_at,
            NEW.deleted_at
        ) IS DISTINCT FROM (
            OLD.state_changed_at,
            OLD.published_at,
            OLD.deleted_at
        ) THEN
            RAISE EXCEPTION 'artifact state timestamps are immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state IN ('published', 'corrupt', 'deleting')) OR
        (OLD.state = 'published' AND NEW.state IN ('corrupt', 'deleting')) OR
        (OLD.state = 'corrupt' AND NEW.state = 'deleting') OR
        (OLD.state = 'deleting' AND NEW.state = 'deleted')
    ) THEN
        RAISE EXCEPTION 'invalid artifact state transition';
    END IF;
    IF NEW.state_changed_at < OLD.state_changed_at THEN
        RAISE EXCEPTION 'artifact state time moved backwards';
    END IF;
    IF NEW.state = 'published' THEN
        IF NEW.published_at IS NULL OR OLD.published_at IS NOT NULL THEN
            RAISE EXCEPTION 'artifact publish timestamp is invalid';
        END IF;
    ELSIF NEW.published_at IS DISTINCT FROM OLD.published_at THEN
        RAISE EXCEPTION 'artifact publish timestamp changed unexpectedly';
    END IF;
    IF NEW.state = 'deleted' THEN
        IF NEW.deleted_at IS NULL OR OLD.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'artifact delete timestamp is invalid';
        END IF;
    ELSIF NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        RAISE EXCEPTION 'artifact delete timestamp changed unexpectedly';
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE artifact_batches
    ENABLE TRIGGER controlled_artifact_batches_update;

ALTER TABLE artifacts
    ENABLE TRIGGER controlled_artifacts_update;
