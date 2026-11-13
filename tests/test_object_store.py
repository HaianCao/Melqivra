"""S3-compatible immutable object-store adapter tests."""

from __future__ import annotations

import hashlib
import io
import unittest
from datetime import UTC, datetime
from typing import Any

from agent_music.object_store import (
    ObjectConflictError,
    ObjectIntegrityError,
    ObjectStoreError,
    S3ObjectStore,
)


class ProviderError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__("provider detail must not cross the adapter")
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


class ReadableBody(io.BytesIO):
    closed_by_adapter = False

    def close(self) -> None:
        self.closed_by_adapter = True
        super().close()


class FakeS3Client:
    def __init__(self, *, bucket_exists: bool = True) -> None:
        self.bucket_exists = bucket_exists
        self.closed = False
        self.created_with: dict[str, Any] | None = None
        self.objects: dict[str, dict[str, Any]] = {}
        self.last_body: ReadableBody | None = None
        self.declared_get_length_override: int | None = None

    def close(self) -> None:
        self.closed = True

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        if not self.bucket_exists:
            raise ProviderError("NoSuchBucket", 404)
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.created_with = kwargs
        self.bucket_exists = True
        return {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if key in self.objects and kwargs.get("IfNoneMatch") == "*":
            raise ProviderError("PreconditionFailed", 412)
        self.objects[key] = {
            "Body": bytes(kwargs["Body"]),
            "ContentLength": int(kwargs["ContentLength"]),
            "ContentType": str(kwargs["ContentType"]),
            "Metadata": dict(kwargs["Metadata"]),
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        }
        return {}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        try:
            item = self.objects[str(kwargs["Key"])]
        except KeyError:
            raise ProviderError("NoSuchKey", 404) from None
        return {key: value for key, value in item.items() if key != "Body"}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        try:
            item = self.objects[str(kwargs["Key"])]
            content = bytes(item["Body"])
        except KeyError:
            raise ProviderError("NoSuchKey", 404) from None
        self.last_body = ReadableBody(content)
        declared_size = self.declared_get_length_override
        if declared_size is None:
            declared_size = int(item["ContentLength"])
        return {"Body": self.last_body, "ContentLength": declared_size}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.objects.pop(str(kwargs["Key"]), None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        prefix = str(kwargs["Prefix"])
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        start = int(kwargs.get("ContinuationToken", "0"))
        page = keys[start : start + 1]
        next_index = start + len(page)
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": self.objects[key]["ContentLength"],
                    "LastModified": self.objects[key]["LastModified"],
                }
                for key in page
            ],
            "IsTruncated": next_index < len(keys),
            "NextContinuationToken": str(next_index) if next_index < len(keys) else None,
        }


class S3ObjectStoreTests(unittest.TestCase):
    def test_create_put_read_list_and_delete_are_bounded_and_immutable(self) -> None:
        client = FakeS3Client(bucket_exists=False)
        store = S3ObjectStore(
            client,
            bucket="melqivra-artifacts",
            region="ap-southeast-1",
            create_bucket=True,
        )
        self.assertEqual(
            {
                "Bucket": "melqivra-artifacts",
                "CreateBucketConfiguration": {"LocationConstraint": "ap-southeast-1"},
            },
            client.created_with,
        )
        first = b"first"
        second = b"second"
        first_sha = hashlib.sha256(first).hexdigest()
        second_sha = hashlib.sha256(second).hexdigest()
        record = store.put_immutable(
            key="artifacts/tenant/project/batch/artifact_one",
            content=first,
            media_type="audio/wav",
            sha256=first_sha,
        )
        store.put_immutable(
            key="artifacts/tenant/project/batch/artifact_two",
            content=second,
            media_type="application/json",
            sha256=second_sha,
        )
        self.assertEqual(first_sha, record.sha256)
        self.assertEqual(first, store.get(record.key))
        assert client.last_body is not None
        self.assertTrue(client.last_body.closed_by_adapter)
        self.assertEqual(
            ["artifact_one", "artifact_two"],
            [item.key.rsplit("/", 1)[-1] for item in store.list("artifacts/tenant/")],
        )
        with self.assertRaises(ObjectIntegrityError):
            store.get(record.key, max_bytes=len(first) - 1)
        assert client.last_body is not None
        self.assertTrue(client.last_body.closed_by_adapter)
        client.declared_get_length_override = len(first) - 1
        with self.assertRaises(ObjectIntegrityError):
            store.get(record.key, max_bytes=len(first) - 1)
        assert client.last_body is not None
        self.assertTrue(client.last_body.closed_by_adapter)
        store.delete(record.key)
        self.assertIsNone(store.stat(record.key))
        store.close()
        self.assertTrue(client.closed)

    def test_exact_retry_is_idempotent_but_changed_content_conflicts(self) -> None:
        client = FakeS3Client()
        store = S3ObjectStore(client, bucket="melqivra-artifacts", region="us-east-1")
        content = b"stable"
        digest = hashlib.sha256(content).hexdigest()
        first = store.put_immutable(
            key="artifacts/tenant/stable",
            content=content,
            media_type="audio/wav",
            sha256=digest,
        )
        self.assertEqual(
            first,
            store.put_immutable(
                key=first.key,
                content=content,
                media_type="audio/wav",
                sha256=digest,
            ),
        )
        changed = b"changed"
        with self.assertRaises(ObjectConflictError):
            store.put_immutable(
                key=first.key,
                content=changed,
                media_type="audio/wav",
                sha256=hashlib.sha256(changed).hexdigest(),
            )

    def test_invalid_inputs_and_provider_failures_do_not_leak_details(self) -> None:
        client = FakeS3Client()
        store = S3ObjectStore(client, bucket="melqivra-artifacts", region="us-east-1")
        with self.assertRaises(ValueError):
            store.get("../private")
        with self.assertRaises(ValueError):
            store.put_immutable(
                key="artifacts/valid",
                content=b"different",
                media_type="audio/wav",
                sha256="0" * 64,
            )

        class FailingClient(FakeS3Client):
            def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
                _ = kwargs
                raise ProviderError("AccessDenied", 403)

        failing = S3ObjectStore(
            FailingClient(),
            bucket="melqivra-artifacts",
            region="us-east-1",
        )
        with self.assertRaisesRegex(ObjectStoreError, "listing failed") as raised:
            failing.list("artifacts/")
        self.assertNotIn("provider detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
