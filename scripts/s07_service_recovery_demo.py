"""Prove the S07 API/worker restart, SSE, FIFO, and tenant boundaries."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import boto3  # type: ignore[import-untyped]
import httpx
import psycopg
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from agent_music.artifact_factory import build_artifact_service
from agent_music.artifact_store import ArtifactWrite
from agent_music.config import HarnessSettings
from agent_music.domain import (
    ArtifactId,
    DocumentType,
    ProjectId,
    RevisionId,
    UserId,
)
from agent_music.models import JobAttempt
from agent_music.postgres_job_store import PostgresJobStore
from agent_music.postgres_project_repository import PostgresProjectRepository

JsonObject = dict[str, Any]
_ALICE = "usr_demo_alice"
_BOB = "usr_demo_bob"
_CAROL = "usr_demo_carol"
_ARTIFACT_ID = "art_s07_demo_audio"
_ARTIFACT_BYTES = b"melqivra-s07-isolated-artifact\n"


@dataclass(frozen=True, slots=True)
class ServiceRecoveryReport:
    """Content-free result of the full S07 service recovery demo."""

    schema_version: int
    status: str
    api_processes_started: int
    worker_processes_started: int
    worker_crashes_injected: int
    tenant_count: int
    job_count: int
    api_worker_separated: bool
    pre_worker_jobs_queued: bool
    completion_order_preserved: bool
    max_concurrent_attempts: int
    recovered_attempt_outcomes: tuple[str, ...]
    successful_attempts_per_job: tuple[int, ...]
    sse_cursor_before_restart: int
    sse_resumed_event_types: tuple[str, ...]
    sse_sequences_contiguous_after_cursor: bool
    tenant_job_isolation: bool
    tenant_artifact_isolation: bool
    artifact_sha256: str
    active_capacity_slots_after: int
    nonterminal_jobs_after: int
    child_processes_after: int


@dataclass(frozen=True, slots=True)
class SseRecord:
    sequence: int
    event_type: str


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_process(
    arguments: list[str],
    *,
    environment: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    with log_path.open("wb") as log_file:
        return subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=environment,
        )


def _stop_process(process: subprocess.Popen[bytes] | None, *, crash: bool) -> None:
    if process is None or process.poll() is not None:
        return
    if crash:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _wait_for_api(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API process exited before becoming ready")
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise TimeoutError("API process did not become ready")


def _response_object(response: httpx.Response) -> JsonObject:
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("API response must be an object")
    return cast(JsonObject, value)


def _user_headers(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def _wait_for_job_state(
    client: httpx.Client,
    *,
    user_id: str,
    job_id: str,
    accepted_states: frozenset[str],
    timeout_seconds: float = 30,
) -> JsonObject:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/gpu/jobs/{job_id}",
            headers=_user_headers(user_id),
        )
        response.raise_for_status()
        body = _response_object(response)
        if body.get("state") in accepted_states:
            return body
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not reach the expected state")


def _read_initial_sse_cursor(
    client: httpx.Client,
    *,
    job_id: str,
) -> tuple[int, tuple[SseRecord, ...]]:
    records: list[SseRecord] = []
    current_sequence: int | None = None
    with client.stream(
        "GET",
        f"/v1/gpu/jobs/{job_id}/events",
        headers=_user_headers(_ALICE),
        timeout=15,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("id: "):
                current_sequence = int(line.removeprefix("id: "))
            elif line.startswith("event: "):
                if current_sequence is None:
                    raise RuntimeError("SSE event did not include an ID")
                record = SseRecord(
                    sequence=current_sequence,
                    event_type=line.removeprefix("event: "),
                )
                records.append(record)
                if record.event_type.startswith("provider."):
                    return record.sequence, tuple(records)
    raise RuntimeError("Initial SSE stream did not reach provider progress")


def _parse_sse(body: str) -> tuple[SseRecord, ...]:
    records: list[SseRecord] = []
    current_sequence: int | None = None
    for line in body.splitlines():
        if line.startswith("id: "):
            current_sequence = int(line.removeprefix("id: "))
        elif line.startswith("event: "):
            if current_sequence is None:
                raise RuntimeError("SSE event did not include an ID")
            records.append(
                SseRecord(
                    sequence=current_sequence,
                    event_type=line.removeprefix("event: "),
                )
            )
    return tuple(records)


def _reset_database(dsn: str) -> None:
    store = PostgresJobStore(dsn)
    store.close()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            TRUNCATE TABLE
                api_rate_limit_counters,
                artifacts,
                artifact_batches,
                tool_audit_events,
                tool_executions,
                approval_requests,
                project_revision_index,
                project_revisions,
                projects,
                gpu_job_events,
                gpu_job_attempts,
                gpu_jobs,
                gpu_capacity_slots
            RESTART IDENTITY
            """
        )
        connection.execute("INSERT INTO gpu_capacity_slots(slot_id) VALUES (1)")
        connection.execute("ALTER SEQUENCE gpu_fencing_token_seq RESTART WITH 1")


def _delete_bucket(endpoint: str, bucket: str) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                contents = page.get("Contents", [])
                for item in contents:
                    client.delete_object(Bucket=bucket, Key=str(item["Key"]))
            client.delete_bucket(Bucket=bucket)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"NoSuchBucket", "404"}:
                raise
    finally:
        client.close()


def _seed_artifact(
    *,
    dsn: str,
    endpoint: str,
    bucket: str,
    transfer_root: Path,
) -> str:
    owner = UserId(_ALICE)
    project_id = ProjectId("prj_s07_service_demo")
    projects = PostgresProjectRepository(dsn)
    try:
        projects.create_project(
            owner_id=owner,
            title="S07 service recovery demo",
            project_id=project_id,
            created_at=1.0,
        )
        revision = projects.commit_revision(
            owner_id=owner,
            project_id=project_id,
            document_type=DocumentType.GENERATION_PLAN,
            payload={"operation": "harness.healthcheck"},
            expected_working_revision_id=None,
            actor="system",
            reason="S07 service recovery evidence",
            revision_id=RevisionId("rev_s07_service_demo"),
            created_at=2.0,
        )
    finally:
        projects.close()

    settings = HarnessSettings(
        runner="fake",
        job_store="postgres",
        postgres_dsn=dsn,
        artifact_storage="s3",
        s3_endpoint_url=endpoint,
        s3_bucket=bucket,
        s3_region="us-east-1",
        s3_create_bucket=True,
        s3_addressing_style="path",
        transfer_root=transfer_root,
    )
    service = build_artifact_service(settings)
    try:
        service.publish_batch(
            owner_id=owner,
            project_id=project_id,
            batch_id="batch_s07_service_demo",
            writes=(
                ArtifactWrite(
                    ArtifactId(_ARTIFACT_ID),
                    "application/octet-stream",
                    _ARTIFACT_BYTES,
                    (revision.id,),
                ),
            ),
        )
    finally:
        service.close()
    return hashlib.sha256(_ARTIFACT_BYTES).hexdigest()


def _service_environment(
    *,
    dsn: str,
    endpoint: str,
    bucket: str,
    runtime_root: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "API_EMBEDDED_WORKER": "false",
            "API_RATE_LIMIT_REQUESTS": "1000",
            "API_RATE_LIMIT_WINDOW_SECONDS": "60",
            "API_SSE_BATCH_SIZE": "100",
            "API_SSE_KEEPALIVE_SECONDS": "0.1",
            "API_SSE_POLL_SECONDS": "0.02",
            "ARTIFACT_ROOT": str(runtime_root / "artifacts"),
            "ARTIFACT_STORAGE": "s3",
            "FAKE_GPU_JOB_SECONDS": "30",
            "GPU_IDLE_TIMEOUT_SECONDS": "60",
            "GPU_JOB_HEARTBEAT_SECONDS": "0.5",
            "GPU_JOB_LEASE_SECONDS": "2",
            "GPU_JOB_MAX_ATTEMPTS": "3",
            "GPU_JOB_TIMEOUT_SECONDS": "60",
            "GPU_RUNNER": "fake",
            "GPU_SCHEDULER_POLL_SECONDS": "0.05",
            "HARNESS_DATABASE_PATH": str(runtime_root / "unused.db"),
            "JOB_STORE": "postgres",
            "POSTGRES_DSN": dsn,
            "POSTGRES_POOL_MAX_SIZE": "4",
            "POSTGRES_POOL_MIN_SIZE": "1",
            "PYTHONUNBUFFERED": "1",
            "S3_ADDRESSING_STYLE": "path",
            "S3_BUCKET": bucket,
            "S3_CREATE_BUCKET": "true",
            "S3_ENDPOINT_URL": endpoint,
            "S3_REGION": "us-east-1",
            "TRANSFER_ROOT": str(runtime_root / "transfers"),
        }
    )
    return environment


def _maximum_concurrent_attempts(attempts: tuple[JobAttempt, ...]) -> int:
    boundaries: list[tuple[float, int]] = []
    for attempt in attempts:
        if attempt.finished_at is None:
            raise RuntimeError("Demo attempt did not converge")
        boundaries.append((attempt.started_at, 1))
        boundaries.append((attempt.finished_at, -1))
    active = 0
    maximum = 0
    for _timestamp, delta in sorted(boundaries, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def run_demo() -> ServiceRecoveryReport:
    """Run the isolated real PostgreSQL/S3 multi-process recovery proof."""

    dsn = _required_environment("MELQIVRA_TEST_POSTGRES_DSN")
    endpoint = _required_environment("MELQIVRA_TEST_S3_ENDPOINT")
    bucket = os.getenv("MELQIVRA_S07_DEMO_BUCKET", "melqivra-s07-recovery-demo").strip()
    if not bucket:
        raise RuntimeError("MELQIVRA_S07_DEMO_BUCKET must not be empty")

    api_process: subprocess.Popen[bytes] | None = None
    worker_process: subprocess.Popen[bytes] | None = None
    replacement_worker: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = _service_environment(
            dsn=dsn,
            endpoint=endpoint,
            bucket=bucket,
            runtime_root=root,
        )
        port = _free_tcp_port()
        base_url = f"http://127.0.0.1:{port}"
        api_arguments = [
            sys.executable,
            "-m",
            "agent_music.cli",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        worker_arguments = [sys.executable, "-m", "agent_music.cli", "worker"]

        _reset_database(dsn)
        _delete_bucket(endpoint, bucket)
        artifact_sha256 = _seed_artifact(
            dsn=dsn,
            endpoint=endpoint,
            bucket=bucket,
            transfer_root=root / "seed-transfers",
        )

        try:
            api_process = _start_process(
                api_arguments,
                environment=environment,
                log_path=root / "api-first.log",
            )
            _wait_for_api(api_process, base_url)
            expected_users = (_ALICE, _BOB, _CAROL)
            job_ids: list[str] = []
            with httpx.Client(base_url=base_url, timeout=10) as client:
                runner = client.get("/v1/gpu/runner", headers=_user_headers(_ALICE))
                runner.raise_for_status()
                api_worker_separated = _response_object(runner).get("worker_mode") == "external"
                for index, user_id in enumerate(expected_users, start=1):
                    response = client.post(
                        "/v1/gpu/jobs",
                        headers=_user_headers(user_id),
                        json={
                            "kind": "harness.healthcheck",
                            "payload": {"sequence": index},
                            "idempotency_key": f"s07-demo-{index}",
                        },
                    )
                    response.raise_for_status()
                    job_ids.append(str(_response_object(response)["id"]))
                time.sleep(0.2)
                queued_states = tuple(
                    str(
                        _response_object(
                            client.get(
                                f"/v1/gpu/jobs/{job_id}",
                                headers=_user_headers(user_id),
                            )
                        )["state"]
                    )
                    for user_id, job_id in zip(expected_users, job_ids, strict=True)
                )
                pre_worker_jobs_queued = queued_states == ("queued", "queued", "queued")

                worker_process = _start_process(
                    worker_arguments,
                    environment=environment,
                    log_path=root / "worker-first.log",
                )
                _wait_for_job_state(
                    client,
                    user_id=_ALICE,
                    job_id=job_ids[0],
                    accepted_states=frozenset({"running"}),
                )
                sse_cursor, initial_records = _read_initial_sse_cursor(
                    client,
                    job_id=job_ids[0],
                )
                if not any(record.event_type.startswith("provider.") for record in initial_records):
                    raise RuntimeError("Initial worker did not publish durable progress")

            _stop_process(api_process, crash=False)
            api_process = None
            api_process = _start_process(
                api_arguments,
                environment=environment,
                log_path=root / "api-restarted.log",
            )
            _wait_for_api(api_process, base_url)

            _stop_process(worker_process, crash=True)
            worker_process = None
            replacement_environment = dict(environment)
            replacement_environment["FAKE_GPU_JOB_SECONDS"] = "0.1"
            replacement_worker = _start_process(
                worker_arguments,
                environment=replacement_environment,
                log_path=root / "worker-replacement.log",
            )

            with httpx.Client(base_url=base_url, timeout=40) as client:
                resumed = client.get(
                    f"/v1/gpu/jobs/{job_ids[0]}/events",
                    headers={
                        **_user_headers(_ALICE),
                        "Last-Event-ID": str(sse_cursor),
                    },
                )
                resumed.raise_for_status()
                resumed_records = _parse_sse(resumed.text)
                if not resumed_records:
                    raise RuntimeError("SSE reconnect returned no durable events")
                resumed_sequences = tuple(record.sequence for record in resumed_records)
                expected_sequences = tuple(
                    range(sse_cursor + 1, sse_cursor + 1 + len(resumed_sequences))
                )
                resumed_types = tuple(record.event_type for record in resumed_records)
                if "job.requeued" not in resumed_types or "job.succeeded" not in resumed_types:
                    raise RuntimeError("SSE reconnect did not include recovery and success")

                final_jobs = tuple(
                    _wait_for_job_state(
                        client,
                        user_id=user_id,
                        job_id=job_id,
                        accepted_states=frozenset({"succeeded"}),
                    )
                    for user_id, job_id in zip(expected_users, job_ids, strict=True)
                )
                completion_order = tuple(
                    str(item["id"])
                    for item in sorted(final_jobs, key=lambda item: float(item["finished_at"]))
                )

                cross_job = client.get(
                    f"/v1/gpu/jobs/{job_ids[0]}",
                    headers=_user_headers(_BOB),
                )
                missing_job = client.get(
                    "/v1/gpu/jobs/job_missing",
                    headers=_user_headers(_BOB),
                )
                tenant_job_isolation = (
                    cross_job.status_code == missing_job.status_code == 404
                    and _response_object(cross_job)["error"]["code"]
                    == _response_object(missing_job)["error"]["code"]
                    == "job_not_found"
                )

                owner_artifact = client.get(
                    f"/v1/artifacts/{_ARTIFACT_ID}",
                    headers=_user_headers(_ALICE),
                )
                cross_artifact = client.get(
                    f"/v1/artifacts/{_ARTIFACT_ID}",
                    headers=_user_headers(_BOB),
                )
                missing_artifact = client.get(
                    "/v1/artifacts/art_missing",
                    headers=_user_headers(_ALICE),
                )
                tenant_artifact_isolation = (
                    owner_artifact.status_code == 200
                    and owner_artifact.content == _ARTIFACT_BYTES
                    and cross_artifact.status_code == missing_artifact.status_code == 404
                    and _response_object(cross_artifact)["error"]["code"]
                    == _response_object(missing_artifact)["error"]["code"]
                    == "artifact_not_found"
                )

            store = PostgresJobStore(dsn, migrate=False)
            try:
                attempts_by_job = tuple(store.list_attempts(job_id) for job_id in job_ids)
                all_attempts = tuple(
                    attempt for attempts in attempts_by_job for attempt in attempts
                )
                recovered_outcomes = tuple(attempt.outcome.value for attempt in attempts_by_job[0])
                successful_attempts = tuple(
                    sum(attempt.outcome.value == "succeeded" for attempt in attempts)
                    for attempts in attempts_by_job
                )
            finally:
                store.close()

            with psycopg.connect(dsn) as connection:
                active_row = connection.execute(
                    """
                    SELECT COUNT(*) FROM gpu_capacity_slots
                    WHERE active_job_id IS NOT NULL
                    """
                ).fetchone()
                nonterminal_row = connection.execute(
                    """
                    SELECT COUNT(*) FROM gpu_jobs
                    WHERE state IN ('queued', 'running')
                    """
                ).fetchone()
                if active_row is None or nonterminal_row is None:
                    raise RuntimeError("Final service-state query returned no row")
                active_capacity_slots = int(active_row[0])
                nonterminal_jobs = int(nonterminal_row[0])

            if recovered_outcomes != ("lease_expired", "succeeded"):
                raise RuntimeError("Recovered job attempts did not converge exactly once")
            if successful_attempts != (1, 1, 1):
                raise RuntimeError("A job completed more or less than once")

            stopped_children = (api_process, worker_process, replacement_worker)
            _stop_process(replacement_worker, crash=False)
            _stop_process(api_process, crash=False)
            child_processes_after = sum(
                process is not None and process.poll() is None for process in stopped_children
            )
            replacement_worker = None
            api_process = None

            return ServiceRecoveryReport(
                schema_version=1,
                status="passed",
                api_processes_started=2,
                worker_processes_started=2,
                worker_crashes_injected=1,
                tenant_count=3,
                job_count=3,
                api_worker_separated=api_worker_separated,
                pre_worker_jobs_queued=pre_worker_jobs_queued,
                completion_order_preserved=completion_order == tuple(job_ids),
                max_concurrent_attempts=_maximum_concurrent_attempts(all_attempts),
                recovered_attempt_outcomes=recovered_outcomes,
                successful_attempts_per_job=successful_attempts,
                sse_cursor_before_restart=sse_cursor,
                sse_resumed_event_types=resumed_types,
                sse_sequences_contiguous_after_cursor=resumed_sequences == expected_sequences,
                tenant_job_isolation=tenant_job_isolation,
                tenant_artifact_isolation=tenant_artifact_isolation,
                artifact_sha256=artifact_sha256,
                active_capacity_slots_after=active_capacity_slots,
                nonterminal_jobs_after=nonterminal_jobs,
                child_processes_after=child_processes_after,
            )
        finally:
            _stop_process(replacement_worker, crash=True)
            _stop_process(worker_process, crash=True)
            _stop_process(api_process, crash=True)
            with contextlib.suppress(Exception):
                _delete_bucket(endpoint, bucket)
            with contextlib.suppress(Exception):
                _reset_database(dsn)


def main() -> int:
    try:
        report = run_demo()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error_type": type(error).__name__,
                    "schema_version": 1,
                    "status": "failed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
