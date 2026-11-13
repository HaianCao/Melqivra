# Service quotas and observability

The S07 HTTP boundary applies quotas before work reaches durable GPU scheduling
and emits operational signals that do not contain tenant identity, creative
content, object keys, credentials, or raw resource identifiers.

## Quota authority

Production mode (`JOB_STORE=postgres`) uses PostgreSQL as the request-rate
authority. One row per opaque SHA-256 principal key is updated atomically against
the database clock, so every API process observes the same fixed window. The row
is overwritten when the window changes; the table does not add a row per request
or per window. A database failure fails closed with the stable
`rate_limit_unavailable` response and does not fall back to a process-local
counter.

SQLite development mode uses a bounded, thread-safe in-memory adapter. It is not
a cross-process production quota and is deliberately selected only with the
development job authority.

The response contract uses the standard fields:

- `RateLimit-Limit`: configured requests per window;
- `RateLimit-Remaining`: remaining accepted requests;
- `RateLimit-Reset`: whole seconds until the current window resets;
- `Retry-After`: present on a 429 response.

Defaults and bounds:

| Setting | Default | Validation |
|---|---:|---:|
| `API_RATE_LIMIT_REQUESTS` | 120 | 1–1,000,000 |
| `API_RATE_LIMIT_WINDOW_SECONDS` | 60 | 1–3,600 |
| `API_MAX_REQUEST_BODY_BYTES` | 131,072 | at least the job-payload limit and at most 16 MiB |
| `API_MAX_JOB_PAYLOAD_BYTES` | 65,536 | at least 2 bytes |
| `API_MAX_ACTIVE_JOBS_PER_USER` | 10 | positive |

The raw body cap is enforced before JSON parsing. The serialized job payload is
then checked independently, and the durable job store atomically enforces the
per-tenant active-job limit. The agent harness separately bounds context tokens,
turns, tool calls, repeated failures, and wall-clock execution; the current S07
HTTP API has no public chat/token-consumption endpoint.

## Metrics and logs

`GET /v1/operations/metrics` requires the same authenticated principal and quota
as every other `/v1/` route. It returns only process start time, active and total
request counts, rate-limited count, status-class counts, and duration aggregates
under this fixed route vocabulary:

- health and API documentation;
- GPU runner, job collection, job item, and job events;
- artifact item;
- operations metrics;
- unknown.

Parameterized paths are classified before aggregation, so job and artifact IDs
cannot become labels. The endpoint is process-local by design; a production
metrics collector may aggregate it across replicas without changing PostgreSQL's
durable authority.

Each completed request writes one structured `melqivra.api` log record containing
only method, status, duration, request ID, trace ID, and bounded route class. The
logger does not receive the raw path, query, tenant ID, request body, response
body, artifact key, or exception detail.

## Trace correlation

The service accepts only W3C version-00 `traceparent` values with nonzero trace
and parent IDs. A valid trace ID and flags are preserved while a new server span
ID is generated. Invalid input is replaced with a new opaque trace. Every HTTP
response returns the resulting `Traceparent` plus `X-Request-ID`.

These values are correlation identifiers, not authorization credentials. They
must not be used to select tenant objects or make policy decisions.

## Schema and recovery

Migration `0009_create_api_rate_limit_counters.sql` is append-only and follows
the repository migration policy. Its canonical SHA-256 is
`20ed5f7d2e0ab352a21bcebc1935c2a15b91a5af4406e3c8266ef935a8d43502`.
Rate counters are operational state and may be restored with the PostgreSQL
backup. Restoring an older window cannot grant more than the configured fixed
window after the next atomic consume because the database clock replaces it.
