ALTER TABLE projects
ADD CONSTRAINT uq_projects_owner_id_id UNIQUE(owner_id, id);

CREATE TABLE approval_requests (
    approval_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (BTRIM(action) <> '' AND LENGTH(action) <= 512),
    risk_class TEXT NOT NULL CHECK (
        risk_class IN ('gpu', 'external', 'identity', 'destructive')
    ),
    decision TEXT NOT NULL CHECK (
        decision IN ('pending', 'approved', 'rejected')
    ),
    created_at DOUBLE PRECISION NOT NULL,
    decided_at DOUBLE PRECISION,
    decision_actor TEXT,
    FOREIGN KEY(user_id, project_id) REFERENCES projects(owner_id, id),
    CHECK (
        (decision = 'pending' AND decided_at IS NULL AND decision_actor IS NULL)
        OR
        (decision IN ('approved', 'rejected')
            AND decided_at IS NOT NULL
            AND decision_actor IS NOT NULL
            AND BTRIM(decision_actor) <> '')
    )
);

CREATE INDEX ix_approval_requests_project_decision
ON approval_requests(user_id, project_id, decision, created_at, approval_id);

CREATE OR REPLACE FUNCTION enforce_approval_request_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.approval_id <> NEW.approval_id
        OR OLD.user_id <> NEW.user_id
        OR OLD.project_id <> NEW.project_id
        OR OLD.action <> NEW.action
        OR OLD.risk_class <> NEW.risk_class
        OR OLD.created_at <> NEW.created_at THEN
        RAISE EXCEPTION 'approval request binding is immutable';
    END IF;
    IF OLD.decision <> 'pending' OR NEW.decision NOT IN ('approved', 'rejected') THEN
        RAISE EXCEPTION 'approval decision is final';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER approval_request_one_way_update
BEFORE UPDATE ON approval_requests
FOR EACH ROW EXECUTE FUNCTION enforce_approval_request_transition();

CREATE OR REPLACE FUNCTION reject_approval_request_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'approval requests are retained';
END;
$$;

CREATE TRIGGER approval_request_no_delete
BEFORE DELETE ON approval_requests
FOR EACH ROW EXECUTE FUNCTION reject_approval_request_delete();

CREATE TABLE tool_executions (
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL CHECK (BTRIM(tool_call_id) <> ''),
    tool_name TEXT NOT NULL CHECK (BTRIM(tool_name) <> ''),
    input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL CHECK (state IN ('running', 'completed')),
    result_json TEXT,
    result_revision_id TEXT,
    actor TEXT NOT NULL CHECK (BTRIM(actor) <> ''),
    turn_id TEXT NOT NULL CHECK (BTRIM(turn_id) <> ''),
    started_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    PRIMARY KEY(user_id, project_id, tool_call_id),
    FOREIGN KEY(user_id, project_id) REFERENCES projects(owner_id, id),
    CHECK (
        (state = 'running' AND result_json IS NULL AND completed_at IS NULL)
        OR
        (state = 'completed' AND result_json IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX ix_tool_executions_project_started
ON tool_executions(user_id, project_id, started_at, tool_call_id);

CREATE OR REPLACE FUNCTION enforce_tool_execution_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.user_id <> NEW.user_id
        OR OLD.project_id <> NEW.project_id
        OR OLD.tool_call_id <> NEW.tool_call_id
        OR OLD.tool_name <> NEW.tool_name
        OR OLD.input_sha256 <> NEW.input_sha256
        OR OLD.actor <> NEW.actor
        OR OLD.turn_id <> NEW.turn_id
        OR OLD.started_at <> NEW.started_at THEN
        RAISE EXCEPTION 'tool execution binding is immutable';
    END IF;
    IF OLD.state <> 'running' OR NEW.state <> 'completed' THEN
        RAISE EXCEPTION 'tool execution transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tool_execution_one_way_update
BEFORE UPDATE ON tool_executions
FOR EACH ROW EXECUTE FUNCTION enforce_tool_execution_transition();

CREATE OR REPLACE FUNCTION enforce_tool_execution_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state <> 'running' THEN
        RAISE EXCEPTION 'completed tool executions are retained';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER tool_execution_running_delete_only
BEFORE DELETE ON tool_executions
FOR EACH ROW EXECUTE FUNCTION enforce_tool_execution_delete();

CREATE TABLE tool_audit_events (
    event_order BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (BTRIM(actor) <> ''),
    tool_name TEXT NOT NULL CHECK (BTRIM(tool_name) <> ''),
    tool_call_id TEXT NOT NULL CHECK (BTRIM(tool_call_id) <> ''),
    outcome TEXT NOT NULL CHECK (BTRIM(outcome) <> ''),
    result_revision_id TEXT,
    turn_id TEXT NOT NULL CHECK (BTRIM(turn_id) <> ''),
    created_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY(user_id, project_id) REFERENCES projects(owner_id, id)
);

CREATE INDEX ix_tool_audit_events_project_order
ON tool_audit_events(user_id, project_id, event_order);

CREATE OR REPLACE FUNCTION reject_tool_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'tool audit events are append-only';
END;
$$;

CREATE TRIGGER tool_audit_no_update
BEFORE UPDATE ON tool_audit_events
FOR EACH ROW EXECUTE FUNCTION reject_tool_audit_mutation();

CREATE TRIGGER tool_audit_no_delete
BEFORE DELETE ON tool_audit_events
FOR EACH ROW EXECUTE FUNCTION reject_tool_audit_mutation();
