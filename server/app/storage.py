"""Where uploaded images are kept.

A single node keeps them on local disk. Several replicas behind a load
balancer cannot: an image uploaded through one would be missing on the others.
Setting ``COLLABDOC_UPLOAD_BUCKET`` moves them to S3, where every replica sees
the same objects. Bytes are still served through the app rather than by
redirecting to S3, so the access check stays in one place and published
pages keep their ``img-src 'self'`` policy.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .config import get_settings


class Storage:
    def put(self, key: str, data: bytes, mime: str) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:
        """The object's bytes, or None when there is no such object."""
        raise NotImplementedError


class LocalStorage(Storage):
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / key

    def put(self, key: str, data: bytes, mime: str) -> None:
        self._path(key).write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None


class S3Storage(Storage):
    """Credentials come from the default AWS chain (on EC2, the instance role)."""

    def __init__(self, bucket: str, prefix: str = "uploads/", client=None) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def put(self, key: str, data: bytes, mime: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self.prefix + key, Body=data, ContentType=mime
        )

    def get(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)
        except self.client.exceptions.NoSuchKey:
            return None
        return response["Body"].read()


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    if settings.upload_bucket:
        return S3Storage(settings.upload_bucket)
    return LocalStorage(settings.upload_dir)
