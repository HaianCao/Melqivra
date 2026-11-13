"""S07 HTTP service-boundary tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from agent_music.api import create_app
from agent_music.config import HarnessSettings
from agent_music.scheduler import GpuHarness


class ApiTenantIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = HarnessSettings(
            database_path=root / "jobs.db",
            artifact_root=root / "artifacts",
            transfer_root=root / "transfers",
            colab_cli_config=root / "colab" / "sessions.json",
            runner="fake",
            fake_job_seconds=1.0,
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

    async def test_cross_tenant_job_access_matches_missing_job(self) -> None:
        created = await self.client.post(
            "/v1/gpu/jobs",
            headers={"X-User-Id": "alice"},
            json={"kind": "harness.healthcheck", "payload": {}},
        )
        self.assertEqual(202, created.status_code, created.text)
        job_id = str(created.json()["id"])

        missing_get = await self.client.get(
            "/v1/gpu/jobs/missing",
            headers={"X-User-Id": "bob"},
        )
        cross_tenant_get = await self.client.get(
            f"/v1/gpu/jobs/{job_id}",
            headers={"X-User-Id": "bob"},
        )
        missing_cancel = await self.client.delete(
            "/v1/gpu/jobs/missing",
            headers={"X-User-Id": "bob"},
        )
        cross_tenant_cancel = await self.client.delete(
            f"/v1/gpu/jobs/{job_id}",
            headers={"X-User-Id": "bob"},
        )

        for response in (
            missing_get,
            cross_tenant_get,
            missing_cancel,
            cross_tenant_cancel,
        ):
            self.assertEqual(404, response.status_code)
            self.assertEqual({"detail": "Job not found"}, response.json())

        owner_get = await self.client.get(
            f"/v1/gpu/jobs/{job_id}",
            headers={"X-User-Id": "alice"},
        )
        self.assertEqual(200, owner_get.status_code, owner_get.text)
        self.assertFalse(owner_get.json()["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
