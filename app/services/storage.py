"""
storage.py — Unified cloud/local storage service.
Supports: MinIO (self-hosted S3), Cloudinary, local filesystem.

Usage:
    from app.services.storage import get_storage
    storage = get_storage()
    url = await storage.upload(file_bytes, key="users/abc/avatar.png", content_type="image/png")
    await storage.delete(key="users/abc/avatar.png")
    data = await storage.download(key="users/abc/avatar.png")
"""
from __future__ import annotations

import io
import logging
import os
import pathlib
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract base for storage backends."""

    @abstractmethod
    async def upload(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload data and return the public/accessible URL."""

    @abstractmethod
    async def download(self, key: str) -> Optional[bytes]:
        """Download object by key.  Returns None if not found."""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete object by key.  Returns True on success."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if key exists."""


# ---------------------------------------------------------------------------
# Local filesystem backend (default / dev)
# ---------------------------------------------------------------------------

class LocalStorageBackend(StorageBackend):
    def __init__(self, base_path: str = "/tmp/pakalon_storage") -> None:
        self._base = pathlib.Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)

    async def upload(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        dest = self._base / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info("[local_storage] Saved %s (%d bytes)", key, len(data))
        return f"local://{key}"

    async def download(self, key: str) -> Optional[bytes]:
        dest = self._base / key
        if dest.exists():
            return dest.read_bytes()
        return None

    async def delete(self, key: str) -> bool:
        dest = self._base / key
        if dest.exists():
            dest.unlink()
            return True
        return False

    async def exists(self, key: str) -> bool:
        return (self._base / key).exists()


# ---------------------------------------------------------------------------
# MinIO (self-hosted S3-compatible) backend
# ---------------------------------------------------------------------------

class MinIOStorageBackend(StorageBackend):
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = True,
    ) -> None:
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._secure = secure
        self._client = None

    def _get_client(self):
        if self._client is None:
            from minio import Minio  # type: ignore
            self._client = Minio(
                self._endpoint,
                access_key=self._access_key,
                secret_key=self._secret_key,
                secure=self._secure,
            )
            # Ensure bucket exists
            try:
                if not self._client.bucket_exists(self._bucket):
                    self._client.make_bucket(self._bucket)
            except Exception as exc:
                logger.warning("[minio] Bucket setup error: %s", exc)
        return self._client

    async def upload(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        """Upload to MinIO and return presigned URL (1 day expiry)."""
        import asyncio
        from datetime import timedelta
        loop = asyncio.get_event_loop()
        client = self._get_client()

        def _do_upload():
            client.put_object(
                self._bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
            url = client.presigned_get_object(
                self._bucket, key, expires=timedelta(days=1)
            )
            return url

        url = await loop.run_in_executor(None, _do_upload)
        logger.info("[minio] Uploaded %s — %d bytes", key, len(data))
        return url

    async def download(self, key: str) -> Optional[bytes]:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()

        def _do_download():
            try:
                response = client.get_object(self._bucket, key)
                return response.read()
            except Exception:
                return None

        return await loop.run_in_executor(None, _do_download)

    async def delete(self, key: str) -> bool:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()

        def _do_delete():
            try:
                client.remove_object(self._bucket, key)
                return True
            except Exception:
                return False

        return await loop.run_in_executor(None, _do_delete)

    async def exists(self, key: str) -> bool:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()

        def _do_stat():
            try:
                client.stat_object(self._bucket, key)
                return True
            except Exception:
                return False

        return await loop.run_in_executor(None, _do_stat)


# ---------------------------------------------------------------------------
# Cloudinary backend
# ---------------------------------------------------------------------------

class CloudinaryStorageBackend(StorageBackend):
    def __init__(
        self,
        cloud_name: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._configured = False

    def _configure(self) -> None:
        if not self._configured:
            import cloudinary  # type: ignore
            cloudinary.config(
                cloud_name=self._cloud_name,
                api_key=self._api_key,
                api_secret=self._api_secret,
                secure=True,
            )
            self._configured = True

    async def upload(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        import asyncio
        self._configure()

        def _do_upload():
            import cloudinary.uploader  # type: ignore
            # Derive public_id from key (strip file extension)
            public_id = key.rsplit(".", 1)[0].replace("/", "_")
            result = cloudinary.uploader.upload(
                io.BytesIO(data),
                public_id=public_id,
                resource_type="auto",
                overwrite=True,
            )
            return result.get("secure_url", "")

        url = await asyncio.get_event_loop().run_in_executor(None, _do_upload)
        logger.info("[cloudinary] Uploaded %s → %s", key, url)
        return url

    async def download(self, key: str) -> Optional[bytes]:
        """Download via the CDN URL (cloudinary doesn't have a direct download API)."""
        import asyncio
        import httpx

        self._configure()
        import cloudinary  # type: ignore
        public_id = key.rsplit(".", 1)[0].replace("/", "_")
        url = cloudinary.CloudinaryImage(public_id).build_url()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.content
        except Exception as exc:
            logger.error("[cloudinary] Download failed for %s: %s", key, exc)
            return None

    async def delete(self, key: str) -> bool:
        import asyncio
        self._configure()

        def _do_delete():
            import cloudinary.uploader  # type: ignore
            public_id = key.rsplit(".", 1)[0].replace("/", "_")
            result = cloudinary.uploader.destroy(public_id)
            return result.get("result") == "ok"

        return await asyncio.get_event_loop().run_in_executor(None, _do_delete)

    async def exists(self, key: str) -> bool:
        import asyncio
        self._configure()

        def _do_check():
            import cloudinary.api  # type: ignore
            public_id = key.rsplit(".", 1)[0].replace("/", "_")
            try:
                cloudinary.api.resource(public_id)
                return True
            except Exception:
                return False

        return await asyncio.get_event_loop().run_in_executor(None, _do_check)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """Return the configured storage backend (cached singleton)."""
    from app.config import get_settings
    settings = get_settings()
    backend = settings.storage_backend.lower()

    if backend == "minio" and settings.minio_endpoint:
        logger.info("[storage] Using MinIO backend: %s", settings.minio_endpoint)
        return MinIOStorageBackend(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
    elif backend == "cloudinary" and settings.cloudinary_cloud_name:
        logger.info("[storage] Using Cloudinary backend")
        return CloudinaryStorageBackend(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret,
        )
    else:
        logger.info("[storage] Using local filesystem backend: %s", settings.local_storage_path)
        return LocalStorageBackend(base_path=settings.local_storage_path)
