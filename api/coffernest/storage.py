"""S3/MinIO access. Presigned URLs are signed against the *public* endpoint (AGENTS.md §5)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from coffernest.config import Settings, get_settings


class Storage(Protocol):
    def ensure_bucket(self) -> None: ...
    def presign_put(self, key: str, mime: str, expires: int = 900) -> str: ...
    def presign_get(self, key: str, filename: str, expires: int = 3600) -> str: ...
    def head(self, key: str) -> int | None: ...
    def put(self, key: str, data: bytes, mime: str) -> None: ...
    def get(self, key: str, max_bytes: int) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def ping(self) -> bool: ...


def _client(settings: Settings, endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


class S3Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self._s3 = _client(settings, settings.s3_endpoint)
        self._public = _client(settings, settings.s3_public_url)

    def ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._s3.create_bucket(Bucket=self.bucket)

    def presign_put(self, key: str, mime: str, expires: int = 900) -> str:
        url: str = self._public.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": mime},
            ExpiresIn=expires,
        )
        return url

    def presign_get(self, key: str, filename: str, expires: int = 3600) -> str:
        safe = filename.replace('"', "")
        url: str = self._public.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'inline; filename="{safe}"',
            },
            ExpiresIn=expires,
        )
        return url

    def head(self, key: str) -> int | None:
        try:
            meta = self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return None
        return int(meta["ContentLength"])

    def put(self, key: str, data: bytes, mime: str) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)

    def get(self, key: str, max_bytes: int) -> bytes:
        obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        data: bytes = obj["Body"].read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("object is larger than allowed")
        return data

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        """Remove every object under ``prefix`` (a household's files). Returns how many."""
        removed = 0
        pages = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        )
        for page in pages:
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:  # a page holds at most 1,000 keys, the delete_objects limit
                self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})
                removed += len(keys)
        return removed

    def ping(self) -> bool:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except Exception:
            return False
        return True


@lru_cache
def get_storage() -> Storage:
    return S3Storage(get_settings())
