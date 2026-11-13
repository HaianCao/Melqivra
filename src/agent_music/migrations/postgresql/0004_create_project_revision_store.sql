CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (BTRIM(title) <> ''),
    created_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_projects_owner_created
    ON projects(owner_id, created_at, id);

CREATE TABLE IF NOT EXISTS project_revisions (
    revision_order BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    document_type TEXT NOT NULL CHECK (
        document_type IN ('brief', 'blueprint', 'lyrics', 'generation_plan')
    ),
    parent_revision_id TEXT,
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    actor TEXT NOT NULL CHECK (BTRIM(actor) <> ''),
    reason TEXT NOT NULL CHECK (BTRIM(reason) <> ''),
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(project_id, document_type, id),
    FOREIGN KEY(project_id, document_type, parent_revision_id)
        REFERENCES project_revisions(project_id, document_type, id)
);

CREATE INDEX IF NOT EXISTS ix_project_revisions_project_type_order
    ON project_revisions(project_id, document_type, created_at, revision_order);

CREATE TABLE IF NOT EXISTS project_revision_index (
    project_id TEXT NOT NULL REFERENCES projects(id),
    document_type TEXT NOT NULL CHECK (
        document_type IN ('brief', 'blueprint', 'lyrics', 'generation_plan')
    ),
    working_revision_id TEXT NOT NULL,
    accepted_revision_id TEXT,
    PRIMARY KEY(project_id, document_type),
    FOREIGN KEY(project_id, document_type, working_revision_id)
        REFERENCES project_revisions(project_id, document_type, id),
    FOREIGN KEY(project_id, document_type, accepted_revision_id)
        REFERENCES project_revisions(project_id, document_type, id)
);

CREATE OR REPLACE FUNCTION reject_project_revision_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'project revisions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS immutable_project_revisions_update ON project_revisions;
CREATE TRIGGER immutable_project_revisions_update
BEFORE UPDATE ON project_revisions
FOR EACH ROW EXECUTE FUNCTION reject_project_revision_mutation();

DROP TRIGGER IF EXISTS immutable_project_revisions_delete ON project_revisions;
CREATE TRIGGER immutable_project_revisions_delete
BEFORE DELETE ON project_revisions
FOR EACH ROW EXECUTE FUNCTION reject_project_revision_mutation();
