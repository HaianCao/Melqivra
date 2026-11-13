"""Immutable object-byte ports and the S3-compatible production adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_OBJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9._=-]*(?:/[a-z0-9][a-z0-9._=-]*)*$")


class ObjectStoreError(RuntimeError):
    """An object-store operation failed without exposing provider details."""


class ObjectConflictError(ObjectStoreError):
    """An immutable object key already exists with different content."""


class ObjectIntegrityError(ObjectStoreError):
    """Stored bytes no longer match their declared immutable contract."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Provider-neutral immutable object metadata."""

    key: str
    size_bytes: int
    sha256: str | None
    media_type: str | None
    last_modified_at: float | None


class ObjectStore(Protocol):
    """Minimal immutable object storage required by artifact publication."""

    def close(self) -> None: ...

    def put_immutable(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        sha256: str,
    ) -> ObjectInfo: ...

    def stat(self, key: str) -> ObjectInfo | None: ...

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str) -> tuple[ObjectInfo, ...]: ...


class S3Client(Protocol):
    """Runtime subset of the boto3 S3 client used by the adapter."""

    def close(self) -> None: ...

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]: ...

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...


class S3ObjectStore:
    """S3/MinIO adapter with conditional immutable writes and checksum metadata."""

    def __init__(
        self,
        client: S3Client,
        *,
        bucket: str,
        region: str,
        create_bucket: bool = False,
    ) -> None:
        if _BUCKET_NAME.fullmatch(bucket) is None or ".." in bucket:
            raise ValueError("S3 bucket name is invalid")
        if not region:
            raise ValueError("S3 region must not be empty")
        self._client = client
        self._bucket = bucket
        self._region = region
        self._ensure_bucket(create_bucket=create_bucket)

    def close(self) -> None:
        """Release provider HTTP resources."""

        self._client.close()

    def put_immutable(
        self,
        *,
        key: str,
        content: bytes,
        media_type: str,
        sha256: str,
    ) -> ObjectInfo:
        """Create one key exactly once or verify an identical prior write."""

        _validate_object_key(key)
        digest = hashlib.sha256(content).hexdigest()
        if digest != sha256 or len(sha256) != 64:
            raise ValueError("Object checksum does not match content")
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentLength=len(content),
                ContentType=media_type,
                Metadata={"sha256": sha256},
                IfNoneMatch="*",
            )
        except Exception as error:
            if _provider_error_code(error) not in {"PreconditionFailed", "412"}:
                raise ObjectStoreError("S3 immutable object write failed") from error
            existing = self.stat(key)
            if not _matches(existing, len(content), sha256, media_type):
                raise ObjectConflictError(
                    "S3 object key has different immutable content"
                ) from error
            assert existing is not None
            return existing

        observed = self.stat(key)
        if not _matches(observed, len(content), sha256, media_type):
            raise ObjectIntegrityError("S3 object metadata changed during publication")
        assert observed is not None
        return observed

    def stat(self, key: str) -> ObjectInfo | None:
        """Read object metadata without downloading bytes."""

        _validate_object_key(key)
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as error:
            if _provider_error_code(error) in {"NoSuchKey", "NotFound", "404"}:
                return None
            raise ObjectStoreError("S3 object metadata lookup failed") from error
        metadata = response.get("Metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        modified = response.get("LastModified")
        return ObjectInfo(
            key=key,
            size_bytes=int(response.get("ContentLength", -1)),
            sha256=(str(metadata["sha256"]) if "sha256" in metadata else None),
            media_type=(
                str(response["ContentType"]) if response.get("ContentType") is not None else None
            ),
            last_modified_at=_timestamp(modified),
        )

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        """Download one object, converting provider failures to safe errors."""

        _validate_object_key(key)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("Object download bound must not be negative")
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response.get("Body")
            if body is None or not hasattr(body, "read"):
                raise ObjectStoreError("S3 object response has no readable body")
            declared_size = response.get("ContentLength")
            try:
                if (
                    max_bytes is not None
                    and isinstance(declared_size, int)
                    and declared_size > max_bytes
                ):
                    raise ObjectIntegrityError("S3 object exceeds its metadata size bound")
                content = body.read() if max_bytes is None else body.read(max_bytes + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except ObjectStoreError:
            raise
        except Exception as error:
            if _provider_error_code(error) in {"NoSuchKey", "NotFound", "404"}:
                raise KeyError(key) from None
            raise ObjectStoreError("S3 object download failed") from error
        if not isinstance(content, bytes):
            raise ObjectStoreError("S3 object body was not bytes")
        if max_bytes is not None and len(content) > max_bytes:
            raise ObjectIntegrityError("S3 object exceeds its metadata size bound")
        return content

    def delete(self, key: str) -> None:
        """Delete one exact key for reconciliation or retention."""

        _validate_object_key(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as error:
            raise ObjectStoreError("S3 object delete failed") from error

    def list(self, prefix: str) -> tuple[ObjectInfo, ...]:
        """List object keys below one validated service-owned prefix."""

        _validate_object_prefix(prefix)
        items: list[ObjectInfo] = []
        token: str | None = None
        while True:
            arguments: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token is not None:
                arguments["ContinuationToken"] = token
            try:
                response = self._client.list_objects_v2(**arguments)
            except Exception as error:
                raise ObjectStoreError("S3 object listing failed") from error
            contents = response.get("Contents", [])
            if not isinstance(contents, list):
                raise ObjectStoreError("S3 object listing returned invalid contents")
            for item in contents:
                if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                    raise ObjectStoreError("S3 object listing returned an invalid item")
                items.append(
                    ObjectInfo(
                        key=item["Key"],
                        size_bytes=int(item.get("Size", -1)),
                        sha256=None,
                        media_type=None,
                        last_modified_at=_timestamp(item.get("LastModified")),
                    )
                )
            if not bool(response.get("IsTruncated")):
                break
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token:
                raise ObjectStoreError("S3 object listing omitted its continuation token")
            token = next_token
        return tuple(items)

    def _ensure_bucket(self, *, create_bucket: bool) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except Exception as error:
            if _provider_error_code(error) not in {"NoSuchBucket", "NotFound", "404"}:
                raise ObjectStoreError("S3 bucket preflight failed") from error
            if not create_bucket:
                raise ObjectStoreError("Configured S3 bucket does not exist") from error
        arguments: dict[str, Any] = {"Bucket": self._bucket}
        if self._region != "us-east-1":
            arguments["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
        try:
            self._client.create_bucket(**arguments)
        except Exception as error:
            raise ObjectStoreError("S3 bucket creation failed") from error


def _matches(
    observed: ObjectInfo | None,
    size_bytes: int,
    sha256: str,
    media_type: str,
) -> bool:
    return (
        observed is not None
        and observed.size_bytes == size_bytes
        and observed.sha256 == sha256
        and observed.media_type == media_type
    )


def _validate_object_key(key: str) -> None:
    if len(key.encode("utf-8")) > 1_024 or _OBJECT_KEY.fullmatch(key) is None:
        raise ValueError("Object key is invalid")


def _validate_object_prefix(prefix: str) -> None:
    if not prefix or prefix.startswith("/") or not prefix.endswith("/"):
        raise ValueError("Object prefix is invalid")
    _validate_object_key(prefix[:-1])


def _provider_error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return ""
    details = response.get("Error")
    if isinstance(details, dict) and details.get("Code") is not None:
        return str(details["Code"])
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict) and metadata.get("HTTPStatusCode") is not None:
        return str(metadata["HTTPStatusCode"])
    return ""


def _timestamp(value: object) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    return None
