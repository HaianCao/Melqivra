ALTER TABLE projects
    ADD CONSTRAINT projects_id_owner_unique UNIQUE(id, owner_id);

CREATE TABLE artifact_batches (
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL CHECK (contract_sha256 ~ '^[0-9a-f]{64}$'),
    artifact_count INTEGER NOT NULL CHECK (artifact_count > 0),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'published', 'corrupt', 'deleting', 'deleted')
    ),
    created_at DOUBLE PRECISION NOT NULL,
    published_at DOUBLE PRECISION,
    deleted_at DOUBLE PRECISION,
    PRIMARY KEY(owner_id, project_id, batch_id),
    FOREIGN KEY(project_id, owner_id) REFERENCES projects(id, owner_id),
    CHECK (state <> 'published' OR published_at IS NOT NULL),
    CHECK ((state = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE INDEX ix_artifact_batches_reconciliation
    ON artifact_batches(state, created_at, owner_id, project_id, batch_id);

CREATE TABLE artifacts (
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    id TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL CHECK (
        media_type ~ '^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$'
    ),
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    source_revision_ids JSONB NOT NULL CHECK (JSONB_TYPEOF(source_revision_ids) = 'array'),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'published', 'corrupt', 'deleting', 'deleted')
    ),
    created_at DOUBLE PRECISION NOT NULL,
    published_at DOUBLE PRECISION,
    deleted_at DOUBLE PRECISION,
    PRIMARY KEY(owner_id, id),
    UNIQUE(owner_id, project_id, batch_id, ordinal),
    FOREIGN KEY(owner_id, project_id, batch_id)
        REFERENCES artifact_batches(owner_id, project_id, batch_id),
    CHECK (state <> 'published' OR published_at IS NOT NULL),
    CHECK ((state = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE INDEX ix_artifacts_owner_project_published
    ON artifacts(owner_id, project_id, state, created_at, id);

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
    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'pending' AND NEW.state IN ('published', 'corrupt', 'deleting')) OR
        (OLD.state = 'published' AND NEW.state IN ('corrupt', 'deleting')) OR
        (OLD.state = 'corrupt' AND NEW.state = 'deleting') OR
        (OLD.state = 'deleting' AND NEW.state = 'deleted')
    ) THEN
        RAISE EXCEPTION 'invalid artifact batch state transition';
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
    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'pending' AND NEW.state IN ('published', 'corrupt', 'deleting')) OR
        (OLD.state = 'published' AND NEW.state IN ('corrupt', 'deleting')) OR
        (OLD.state = 'corrupt' AND NEW.state = 'deleting') OR
        (OLD.state = 'deleting' AND NEW.state = 'deleted')
    ) THEN
        RAISE EXCEPTION 'invalid artifact state transition';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION reject_artifact_deletion()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'artifact metadata is append-only';
END;
$$;

CREATE TRIGGER controlled_artifact_batches_update
BEFORE UPDATE ON artifact_batches
FOR EACH ROW EXECUTE FUNCTION enforce_artifact_batch_transition();

CREATE TRIGGER immutable_artifact_batches_delete
BEFORE DELETE ON artifact_batches
FOR EACH ROW EXECUTE FUNCTION reject_artifact_deletion();

CREATE TRIGGER controlled_artifacts_update
BEFORE UPDATE ON artifacts
FOR EACH ROW EXECUTE FUNCTION enforce_artifact_transition();

CREATE TRIGGER immutable_artifacts_delete
BEFORE DELETE ON artifacts
FOR EACH ROW EXECUTE FUNCTION reject_artifact_deletion();
