CREATE TABLE api_rate_limit_counters (
    principal_sha256 TEXT PRIMARY KEY,
    window_started_at DOUBLE PRECISION NOT NULL,
    request_count BIGINT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    CONSTRAINT api_rate_limit_principal_sha256 CHECK (
        principal_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT api_rate_limit_window_non_negative CHECK (
        window_started_at >= 0
    ),
    CONSTRAINT api_rate_limit_request_count_positive CHECK (
        request_count >= 1
    ),
    CONSTRAINT api_rate_limit_time_order CHECK (
        updated_at >= window_started_at
    )
);

CREATE INDEX ix_api_rate_limit_counters_updated_at
    ON api_rate_limit_counters(updated_at);
