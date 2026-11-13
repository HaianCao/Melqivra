"""S07 authentication, error, quota, pagination, and tenant-isolation tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from agent_music.api import create_app
from agent_music.artifact_service import ArtifactDownload
from agent_music.authentication import StaticBearerAuthenticator
from agent_music.config import HarnessSettings
from agent_music.domain import ArtifactId, ProjectId, UserId
from agent_music.job_store import SqliteJobStore
from agent_music.object_store import ObjectIntegrityError
from agent_music.scheduler import GpuHarness


def _sse_values(body: str, field: str) -> list[str]:
    prefix = f"{field}: "
    return [line.removeprefix(prefix) for line in body.splitlines() if line.startswith(prefix)]


class ApiServiceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = HarnessSettings(
            database_path=root / "jobs.db",
            artifact_root=root / "artifacts",
            transfer_root=root / "transfers",
            colab_cli_config=root / "colab" / "sessions.json",
            runner="fake",
            fake_job_seconds=5.0,
            api_max_active_jobs_per_user=3,
            api_max_job_payload_bytes=64,
            api_max_page_size=2,
        )
        self.harness = GpuHarness(self.settings)
        await self.harness.start()
        app = create_app(self.settings, self.harness)
        app.state.harness = self.harness
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.harness.close()
        self.temporary.cleanup()

    @staticmethod
    def _headers(user_id: str, *, request_id: str | None = None) -> dict[str, str]:
        headers = {"X-User-Id": user_id}
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        return headers

    async def _submit(
        self,
        user_id: str,
        *,
        idempotency_key: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        body: dict[str, object] = {
            "kind": "harness.healthcheck",
            "payload": payload or {},
        }
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        return await self.client.post(
            "/v1/gpu/jobs",
            headers=self._headers(user_id),
            json=body,
        )

    async def test_cross_tenant_job_access_matches_missing_job(self) -> None:
        created = await self._submit("alice", idempotency_key="alice-one")
        self.assertEqual(202, created.status_code, created.text)
        job_id = str(created.json()["id"])
        self.assertEqual(0, created.json()["attempt_count"])
        self.assertEqual(3, created.json()["max_attempts"])

        responses = (
            await self.client.get(
                "/v1/gpu/jobs/missing",
                headers=self._headers("bob"),
            ),
            await self.client.get(
                f"/v1/gpu/jobs/{job_id}",
                headers=self._headers("bob"),
            ),
            await self.client.delete(
                "/v1/gpu/jobs/missing",
                headers=self._headers("bob"),
            ),
            await self.client.delete(
                f"/v1/gpu/jobs/{job_id}",
                headers=self._headers("bob"),
            ),
        )
        for response in responses:
            self.assertEqual(404, response.status_code)
            self.assertEqual("job_not_found", response.json()["error"]["code"])
            self.assertEqual(
                response.headers["X-Request-ID"], response.json()["error"]["request_id"]
            )

        owner_get = await self.client.get(
            f"/v1/gpu/jobs/{job_id}",
            headers=self._headers("alice"),
        )
        self.assertEqual(200, owner_get.status_code, owner_get.text)
        self.assertFalse(owner_get.json()["cancel_requested"])
        self.assertNotIn("lease_owner", owner_get.json())
        self.assertNotIn("lease_token", owner_get.json())

    async def test_request_ids_authentication_and_validation_errors_are_stable(self) -> None:
        missing = await self.client.get("/v1/gpu/jobs")
        self.assertEqual(401, missing.status_code)
        self.assertEqual("authentication_required", missing.json()["error"]["code"])
        self.assertEqual(missing.headers["X-Request-ID"], missing.json()["error"]["request_id"])

        supplied = await self.client.get(
            "/v1/gpu/jobs",
            headers=self._headers("alice", request_id="client-request-123"),
        )
        self.assertEqual(200, supplied.status_code, supplied.text)
        self.assertEqual("client-request-123", supplied.headers["X-Request-ID"])
        self.assertEqual("nosniff", supplied.headers["X-Content-Type-Options"])
        self.assertEqual("no-store", supplied.headers["Cache-Control"])

        openapi = (await self.client.get("/openapi.json")).json()
        self.assertIn(
            "DevelopmentUserHeader",
            openapi["components"]["securitySchemes"],
        )
        self.assertEqual(
            [{"DevelopmentUserHeader": []}],
            openapi["paths"]["/v1/gpu/jobs"]["get"]["security"],
        )

        unknown = await self.client.get(
            "/v1/unknown",
            headers=self._headers("alice", request_id="route-missing"),
        )
        self.assertEqual(404, unknown.status_code)
        self.assertEqual("route_not_found", unknown.json()["error"]["code"])
        self.assertEqual("route-missing", unknown.json()["error"]["request_id"])

        invalid = await self.client.post(
            "/v1/gpu/jobs",
            headers=self._headers("alice", request_id="unsafe request\nvalue"),
            json={"kind": "", "payload": {}, "unexpected": True},
        )
        self.assertEqual(422, invalid.status_code)
        self.assertEqual("invalid_request", invalid.json()["error"]["code"])
        self.assertRegex(invalid.headers["X-Request-ID"], r"^req_[0-9a-f]{32}$")

    async def test_payload_active_job_and_idempotency_quotas(self) -> None:
        too_large = await self._submit("alice", payload={"text": "x" * 100})
        self.assertEqual(413, too_large.status_code)
        self.assertEqual("payload_too_large", too_large.json()["error"]["code"])

        first = await self._submit("alice", idempotency_key="quota-one")
        second = await self._submit("alice", idempotency_key="quota-two")
        third = await self._submit("alice", idempotency_key="quota-three")
        for response in (first, second, third):
            self.assertEqual(202, response.status_code, response.text)

        replay = await self._submit("alice", idempotency_key="quota-one")
        self.assertEqual(202, replay.status_code, replay.text)
        self.assertEqual(first.json()["id"], replay.json()["id"])

        exceeded = await self._submit("alice", idempotency_key="quota-four")
        self.assertEqual(429, exceeded.status_code)
        self.assertEqual("active_job_quota_exceeded", exceeded.json()["error"]["code"])

        other_tenant = await self._submit("bob", idempotency_key="quota-one")
        self.assertEqual(202, other_tenant.status_code, other_tenant.text)

    async def test_tenant_scoped_cursor_pagination(self) -> None:
        created = [
            await self._submit("alice", idempotency_key=f"page-{index}") for index in range(3)
        ]
        ids = [str(response.json()["id"]) for response in created]
        first = await self.client.get(
            "/v1/gpu/jobs?limit=2",
            headers=self._headers("alice"),
        )
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(ids[:2], [item["id"] for item in first.json()["items"]])
        cursor = str(first.json()["next_cursor"])
        self.assertEqual(ids[1], cursor)

        second = await self.client.get(
            f"/v1/gpu/jobs?limit=2&cursor={cursor}",
            headers=self._headers("alice"),
        )
        self.assertEqual(ids[2:], [item["id"] for item in second.json()["items"]])
        self.assertIsNone(second.json()["next_cursor"])

        cross_tenant = await self.client.get(
            f"/v1/gpu/jobs?limit=2&cursor={cursor}",
            headers=self._headers("bob"),
        )
        self.assertEqual(400, cross_tenant.status_code)
        self.assertEqual("invalid_cursor", cross_tenant.json()["error"]["code"])
        oversized = await self.client.get(
            "/v1/gpu/jobs?limit=3",
            headers=self._headers("alice"),
        )
        self.assertEqual(400, oversized.status_code)
        self.assertEqual("invalid_page_size", oversized.json()["error"]["code"])

    async def test_sse_reconnect_is_durable_and_tenant_scoped(self) -> None:
        event_settings = replace(
            self.settings,
            database_path=Path(self.temporary.name) / "events.db",
            api_sse_poll_seconds=0.01,
            api_sse_keepalive_seconds=0.02,
        )
        event_store = SqliteJobStore(event_settings.database_path)
        event_harness = GpuHarness(event_settings, store=event_store)
        app = create_app(event_settings, event_harness)
        app.state.harness = event_harness
        try:
            job = event_harness.enqueue(
                user_id="alice",
                kind="harness.healthcheck",
                payload={},
            )
            await event_harness.cancel_for_user(user_id="alice", job_id=job.id)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                initial = await client.get(
                    f"/v1/gpu/jobs/{job.id}/events",
                    headers=self._headers("alice"),
                )
                self.assertEqual(200, initial.status_code, initial.text)
                self.assertTrue(initial.headers["content-type"].startswith("text/event-stream"))
                self.assertEqual("no", initial.headers["X-Accel-Buffering"])
                self.assertEqual(["1", "2"], _sse_values(initial.text, "id"))
                self.assertEqual(
                    ["job.queued", "job.cancelled"],
                    _sse_values(initial.text, "event"),
                )
                payloads = [json.loads(value) for value in _sse_values(initial.text, "data")]
                self.assertEqual("cancelled", payloads[-1]["job_state"])

                resumed = await client.get(
                    f"/v1/gpu/jobs/{job.id}/events",
                    headers={**self._headers("alice"), "Last-Event-ID": "1"},
                )
                self.assertEqual(["2"], _sse_values(resumed.text, "id"))

                cross_tenant = await client.get(
                    f"/v1/gpu/jobs/{job.id}/events",
                    headers=self._headers("bob"),
                )
                self.assertEqual(404, cross_tenant.status_code)
                self.assertEqual("job_not_found", cross_tenant.json()["error"]["code"])
                invalid = await client.get(
                    f"/v1/gpu/jobs/{job.id}/events",
                    headers={**self._headers("alice"), "Last-Event-ID": "3"},
                )
                self.assertEqual(400, invalid.status_code)
                self.assertEqual("invalid_event_cursor", invalid.json()["error"]["code"])
        finally:
            await event_harness.close()

    async def test_artifact_download_is_tenant_scoped_and_integrity_safe(self) -> None:
        class Reader:
            def read_for_owner(
                self,
                *,
                owner_id: UserId,
                artifact_id: ArtifactId,
            ) -> ArtifactDownload:
                if owner_id != UserId("alice") or artifact_id != ArtifactId("art_audio"):
                    raise KeyError(str(artifact_id))
                return ArtifactDownload(
                    artifact_id=artifact_id,
                    project_id=ProjectId("prj_song"),
                    media_type="audio/wav",
                    size_bytes=5,
                    sha256="a" * 64,
                    content=b"audio",
                )

        app = create_app(self.settings, self.harness, artifact_reader=Reader())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            owned = await client.get(
                "/v1/artifacts/art_audio",
                headers=self._headers("alice"),
            )
            self.assertEqual(200, owned.status_code, owned.text)
            self.assertEqual(b"audio", owned.content)
            self.assertEqual('"sha256:' + "a" * 64 + '"', owned.headers["etag"])
            cross_tenant = await client.get(
                "/v1/artifacts/art_audio",
                headers=self._headers("bob"),
            )
            missing = await client.get(
                "/v1/artifacts/art_missing",
                headers=self._headers("alice"),
            )
            for response in (cross_tenant, missing):
                self.assertEqual(404, response.status_code)
                self.assertEqual("artifact_not_found", response.json()["error"]["code"])

        class CorruptReader(Reader):
            def read_for_owner(
                self,
                *,
                owner_id: UserId,
                artifact_id: ArtifactId,
            ) -> ArtifactDownload:
                _ = owner_id, artifact_id
                raise ObjectIntegrityError("private storage detail")

        corrupt_app = create_app(
            self.settings,
            self.harness,
            artifact_reader=CorruptReader(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=corrupt_app),
            base_url="http://testserver",
        ) as client:
            unavailable = await client.get(
                "/v1/artifacts/art_audio",
                headers=self._headers("alice"),
            )
            self.assertEqual(503, unavailable.status_code)
            self.assertEqual("artifact_unavailable", unavailable.json()["error"]["code"])
            self.assertNotIn("private storage detail", unavailable.text)

    async def test_injected_bearer_authenticator_ignores_development_identity(self) -> None:
        injected_settings = replace(self.settings, api_auth_mode="injected")
        token = "test-token-with-safe-length"
        authenticator = StaticBearerAuthenticator({token: UserId("usr_bearer")})
        app = create_app(injected_settings, self.harness, authenticator)
        app.state.harness = self.harness
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            rejected = await client.get(
                "/v1/gpu/jobs",
                headers={"X-User-Id": "usr_bearer"},
            )
            self.assertEqual(401, rejected.status_code)
            accepted = await client.get(
                "/v1/gpu/jobs",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(200, accepted.status_code, accepted.text)
            openapi = (await client.get("/openapi.json")).json()
            self.assertIn("BearerAuth", openapi["components"]["securitySchemes"])

        with self.assertRaisesRegex(RuntimeError, "requires an Authenticator"):
            create_app(injected_settings, self.harness)


if __name__ == "__main__":
    unittest.main()
