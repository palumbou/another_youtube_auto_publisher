"""Object storage behind one small interface: a directory tree locally, S3 in AWS.

Keys always use the S3 layout (`incoming/{project_key}/{job_id}/...`). The
filesystem adapter derives a version id from the content so that a replaced
object is detected exactly like a new S3 VersionId.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    version_id: str
    last_modified: datetime
    etag: str = ""


class UnsafeKeyError(ValueError):
    pass


def check_key(key: str) -> str:
    """Reject traversal, absolute paths and control characters before touching storage."""
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise UnsafeKeyError(f"unsafe object key: {key!r}")
    parts = key.rstrip("/").split("/")
    if any(p in ("..", ".", "") for p in parts) or any(ord(c) < 32 for c in key):
        raise UnsafeKeyError(f"unsafe object key: {key!r}")
    return key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ObjectStore(ABC):
    @abstractmethod
    def head(self, key: str) -> ObjectInfo | None: ...

    @abstractmethod
    def list(self, prefix: str) -> list[ObjectInfo]: ...

    @abstractmethod
    def get_bytes(self, key: str, version_id: str | None = None) -> bytes: ...

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> ObjectInfo: ...

    @abstractmethod
    def download(self, key: str, target: Path, version_id: str | None = None) -> Path: ...

    @abstractmethod
    def upload_file(self, source: Path, key: str, content_type: str = "application/octet-stream") -> ObjectInfo: ...

    @abstractmethod
    def sha256(self, key: str, version_id: str | None = None) -> str: ...

    @abstractmethod
    def signed_url(self, key: str, expires_seconds: int = 600) -> str: ...

    def exists(self, key: str) -> bool:
        return self.head(key) is not None


class FilesystemObjectStore(ObjectStore):
    """A directory is the bucket. Version ids are content hashes, so a rewrite is visible."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        check_key(key)
        path = (self.root / key).resolve()
        if self.root not in path.parents and path != self.root:
            raise UnsafeKeyError(f"key escapes the bucket root: {key!r}")
        return path

    def _info(self, key: str, path: Path) -> ObjectInfo:
        stat = path.stat()
        digest = sha256_file(path)
        return ObjectInfo(key=key, size=stat.st_size, version_id="fs-" + digest[:24],
                          last_modified=datetime.fromtimestamp(stat.st_mtime, UTC), etag=digest[:32])

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        return self._info(key, path) if path.is_file() else None

    def list(self, prefix: str) -> list[ObjectInfo]:
        check_key(prefix)
        base = self._path(prefix.rstrip("/")) if prefix else self.root
        if not base.is_dir():
            return []
        out = []
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            key = path.relative_to(self.root).as_posix()
            out.append(self._info(key, path))
        return out

    def get_bytes(self, key: str, version_id: str | None = None) -> bytes:
        path = self._path(key)
        data = path.read_bytes()
        if version_id and "fs-" + hashlib.sha256(data).hexdigest()[:24] != version_id:
            raise FileNotFoundError(f"{key}: version {version_id} is no longer the current object")
        return data

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> ObjectInfo:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return self._info(key, path)

    def download(self, key: str, target: Path, version_id: str | None = None) -> Path:
        path = self._path(key)
        info = self._info(key, path)
        if version_id and info.version_id != version_id:
            raise FileNotFoundError(f"{key}: version {version_id} is no longer the current object")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        return target

    def upload_file(self, source: Path, key: str, content_type: str = "application/octet-stream") -> ObjectInfo:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        shutil.copyfile(source, tmp)
        os.replace(tmp, path)
        return self._info(key, path)

    def sha256(self, key: str, version_id: str | None = None) -> str:
        path = self._path(key)
        digest = sha256_file(path)
        if version_id and "fs-" + digest[:24] != version_id:
            raise FileNotFoundError(f"{key}: version {version_id} is no longer the current object")
        return digest

    def signed_url(self, key: str, expires_seconds: int = 600) -> str:
        # The local console serves objects itself through a short-lived token route.
        return f"/media/{key}"


class S3ObjectStore(ObjectStore):
    def __init__(self, bucket: str, client=None):
        import boto3

        self.bucket = bucket
        self.client = client or boto3.client("s3")

    @staticmethod
    def _to_info(key: str, response: dict) -> ObjectInfo:
        return ObjectInfo(key=key, size=int(response.get("ContentLength", response.get("Size", 0))),
                          version_id=response.get("VersionId", "null"),
                          last_modified=response["LastModified"].astimezone(UTC),
                          etag=response.get("ETag", "").strip('"'))

    def head(self, key: str) -> ObjectInfo | None:
        check_key(key)
        try:
            return self._to_info(key, self.client.head_object(Bucket=self.bucket, Key=key))
        except self.client.exceptions.ClientError as exc:  # pragma: no cover - boto specific
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise

    def list(self, prefix: str) -> list[ObjectInfo]:
        check_key(prefix)
        out = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                out.append(ObjectInfo(key=item["Key"], size=int(item["Size"]), version_id="",
                                      last_modified=item["LastModified"].astimezone(UTC),
                                      etag=item.get("ETag", "").strip('"')))
        return out

    def _version_kwargs(self, version_id: str | None) -> dict:
        return {"VersionId": version_id} if version_id and version_id != "null" else {}

    def get_bytes(self, key: str, version_id: str | None = None) -> bytes:
        check_key(key)
        return self.client.get_object(Bucket=self.bucket, Key=key, **self._version_kwargs(version_id))["Body"].read()

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> ObjectInfo:
        check_key(key)
        response = self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return ObjectInfo(key=key, size=len(data), version_id=response.get("VersionId", "null"),
                          last_modified=datetime.now(UTC), etag=response.get("ETag", "").strip('"'))

    def download(self, key: str, target: Path, version_id: str | None = None) -> Path:
        check_key(key)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(target), ExtraArgs=self._version_kwargs(version_id) or None)
        return target

    def upload_file(self, source: Path, key: str, content_type: str = "application/octet-stream") -> ObjectInfo:
        check_key(key)
        self.client.upload_file(str(source), self.bucket, key, ExtraArgs={"ContentType": content_type})
        info = self.head(key)
        assert info is not None
        return info

    def sha256(self, key: str, version_id: str | None = None) -> str:
        check_key(key)
        body = self.client.get_object(Bucket=self.bucket, Key=key, **self._version_kwargs(version_id))["Body"]
        digest = hashlib.sha256()
        while chunk := body.read(CHUNK):
            digest.update(chunk)
        return digest.hexdigest()

    def signed_url(self, key: str, expires_seconds: int = 600) -> str:
        check_key(key)
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_seconds
        )
