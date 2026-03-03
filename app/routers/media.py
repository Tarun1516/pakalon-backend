"""
T-CLI-P12: Media generation endpoints — image & video (Pro-only).
T-MEDIA-03: MinIO / Cloudinary durable media storage + 7-day signed URLs.

Delegates to external AI APIs in priority order:
  Image: fal.ai → OpenAI DALL-E 3 → Stability AI → Replicate
  Video: fal.ai → Replicate → Runway Gen-3

After generation, the binary is uploaded to:
  1. MinIO (S3-compatible, self-hosted) if MINIO_ENDPOINT is set, OR
  2. Cloudinary if CLOUDINARY_CLOUD_NAME is set.
Returns a 7-day presigned URL (MinIO) or a Cloudinary CDN URL.
"""
from __future__ import annotations

import base64
import os
import pathlib
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.models.user import User

# ---------------------------------------------------------------------------
# T-MEDIA-03: Durable media storage helpers
# ---------------------------------------------------------------------------

_MINIO_ENDPOINT  = os.environ.get("MINIO_ENDPOINT", "")   # e.g. "http://localhost:9000"
_MINIO_ACCESS    = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
_MINIO_SECRET    = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
_MINIO_BUCKET    = os.environ.get("MINIO_BUCKET", "pakalon-media")

_CLOUDINARY_CLOUD  = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
_CLOUDINARY_KEY    = os.environ.get("CLOUDINARY_API_KEY", "")
_CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

_SIGNED_URL_TTL = 7 * 24 * 3600  # 7 days in seconds


async def _upload_to_minio(data: bytes, filename: str) -> str | None:
    """
    Upload *data* to MinIO bucket and return a 7-day presigned GET URL.
    Uses the MinIO S3-compatible API directly over HTTP.
    """
    if not _MINIO_ENDPOINT:
        return None
    try:
        import hmac
        import hashlib
        from datetime import datetime, timezone

        endpoint = _MINIO_ENDPOINT.rstrip("/")
        object_key = f"generated/{filename}"

        # MinIO supports direct presigned URL generation via its S3 API.
        # We use boto3 if available, otherwise fall back to a raw PUT + GET.
        try:
            import boto3  # type: ignore
            from botocore.client import Config  # type: ignore

            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=_MINIO_ACCESS,
                aws_secret_access_key=_MINIO_SECRET,
                config=Config(signature_version="s3v4"),
                region_name="us-east-1",
            )
            # Ensure bucket exists
            try:
                s3.head_bucket(Bucket=_MINIO_BUCKET)
            except Exception:
                s3.create_bucket(Bucket=_MINIO_BUCKET)

            # Upload object
            s3.put_object(
                Bucket=_MINIO_BUCKET,
                Key=object_key,
                Body=data,
                ContentType="image/png" if filename.endswith(".png") else "video/mp4",
            )

            # Generate 7-day presigned URL
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": _MINIO_BUCKET, "Key": object_key},
                ExpiresIn=_SIGNED_URL_TTL,
            )
            return url

        except ImportError:
            # Raw HTTP PUT to MinIO (no boto3)
            content_type = "image/png" if filename.endswith(".png") else "video/mp4"
            async with httpx.AsyncClient(timeout=60) as client:
                put_resp = await client.put(
                    f"{endpoint}/{_MINIO_BUCKET}/{object_key}",
                    content=data,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(len(data)),
                    },
                    auth=(_MINIO_ACCESS, _MINIO_SECRET),
                )
                if put_resp.status_code not in (200, 201, 204):
                    return None
            # Return direct URL (no signing without boto3)
            return f"{endpoint}/{_MINIO_BUCKET}/{object_key}"

    except Exception:
        return None


async def _upload_to_cloudinary(data: bytes, filename: str) -> str | None:
    """
    Upload *data* to Cloudinary and return the secure CDN URL.
    Uses Cloudinary Upload API: POST https://api.cloudinary.com/v1_1/{cloud}/auto/upload
    """
    if not (_CLOUDINARY_CLOUD and _CLOUDINARY_KEY and _CLOUDINARY_SECRET):
        return None
    try:
        import hashlib
        import hmac

        timestamp = str(int(time.time()))
        resource_type = "video" if filename.endswith(".mp4") else "image"
        public_id = f"pakalon/{filename.replace('.', '_')}_{timestamp}"

        # Build signature: SHA-1(public_id=...&timestamp=...&SECRET)
        sig_str = f"public_id={public_id}&timestamp={timestamp}{_CLOUDINARY_SECRET}"
        signature = hashlib.sha1(sig_str.encode()).hexdigest()

        upload_url = f"https://api.cloudinary.com/v1_1/{_CLOUDINARY_CLOUD}/{resource_type}/upload"
        b64_data = base64.b64encode(data).decode()

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                upload_url,
                data={
                    "file": f"data:{('image/png' if resource_type=='image' else 'video/mp4')};base64,{b64_data}",
                    "api_key": _CLOUDINARY_KEY,
                    "timestamp": timestamp,
                    "public_id": public_id,
                    "signature": signature,
                },
            )
            if resp.status_code != 200:
                return None
            result = resp.json()
            return result.get("secure_url")
    except Exception:
        return None


async def _store_media(local_path: str) -> str | None:
    """
    T-MEDIA-03: Upload a locally saved file to durable storage.
    Tries MinIO first, then Cloudinary.
    Returns the CDN/presigned URL or None if neither is configured.
    """
    try:
        file_bytes = pathlib.Path(local_path).read_bytes()
        filename = pathlib.Path(local_path).name

        # Try MinIO
        url = await _upload_to_minio(file_bytes, filename)
        if url:
            return url

        # Try Cloudinary
        url = await _upload_to_cloudinary(file_bytes, filename)
        if url:
            return url

    except Exception:
        pass
    return None

router = APIRouter(prefix="/media", tags=["Media Generation"])

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = 120.0  # seconds


def _headers_fal() -> dict[str, str]:
    key = os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY", "")
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def _headers_openai() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}",
        "Content-Type": "application/json",
    }


def _headers_stability() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('STABILITY_API_KEY', '')}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _headers_replicate() -> dict[str, str]:
    return {
        "Authorization": f"Token {os.environ.get('REPLICATE_API_TOKEN', '')}",
        "Content-Type": "application/json",
    }


def _headers_runway() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('RUNWAYML_API_SECRET', '')}",
        "Content-Type": "application/json",
        "X-Runway-Version": "2024-11-06",
    }


def _save_b64(data: str, ext: str, out: str | None) -> str:
    raw = base64.b64decode(data)
    out_path = out or f"/tmp/pakalon-gen-{uuid.uuid4().hex[:8]}.{ext}"
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out_path).write_bytes(raw)
    return out_path


async def _poll_replicate(prediction_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Poll a Replicate prediction until SUCCEEDED or FAILED."""
    for _ in range(60):
        r = await client.get(
            f"https://api.replicate.com/v1/predictions/{prediction_id}",
            headers=_headers_replicate(),
        )
        data = r.json()
        if data.get("status") in ("succeeded", "failed", "canceled"):
            return data
        await __import__("asyncio").sleep(3)
    return {"status": "timeout"}


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

IMAGE_PROVIDERS = ["fal", "openai", "stability", "replicate"]
IMAGE_MODELS = {
    "fal": "fal-ai/flux/dev",
    "openai": "dall-e-3",
    "stability": "sd3-large-turbo",
    "replicate": "black-forest-labs/flux-schnell",
}


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    output_path: str | None = None
    model: str | None = None          # provider-specific override
    width: int = Field(1024, ge=64, le=2048)
    height: int = Field(1024, ge=64, le=2048)
    steps: int = Field(28, ge=1, le=100)
    guidance: float = Field(3.5, ge=0.0, le=20.0)
    provider: str | None = None       # force a specific provider


class ImageGenerateResponse(BaseModel):
    success: bool
    file_path: str | None = None
    url: str | None = None
    provider: str | None = None
    error: str | None = None


async def _image_fal(req: ImageGenerateRequest) -> ImageGenerateResponse:
    model = req.model or IMAGE_MODELS["fal"]
    payload = {
        "prompt": req.prompt,
        "image_size": {"width": req.width, "height": req.height},
        "num_inference_steps": req.steps,
        "guidance_scale": req.guidance,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post(f"https://fal.run/{model}", json=payload, headers=_headers_fal())
    if r.status_code != 200:
        return ImageGenerateResponse(success=False, provider="fal", error=r.text[:300])
    data = r.json()
    images = data.get("images") or []
    if images:
        url = images[0].get("url")
        if url:
            async with httpx.AsyncClient(timeout=60) as dl:
                img_r = await dl.get(url)
            path = _save_b64(base64.b64encode(img_r.content).decode(), "png", req.output_path)
            return ImageGenerateResponse(success=True, file_path=path, url=url, provider="fal")
    return ImageGenerateResponse(success=False, provider="fal", error="No image in fal response")


async def _image_openai(req: ImageGenerateRequest) -> ImageGenerateResponse:
    size = f"{req.width}x{req.height}"
    # DALL-E 3 only supports specific sizes
    allowed = {"256x256", "512x512", "1024x1024", "1024x1792", "1792x1024"}
    if size not in allowed:
        size = "1024x1024"
    payload = {
        "model": req.model or "dall-e-3",
        "prompt": req.prompt,
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post("https://api.openai.com/v1/images/generations", json=payload, headers=_headers_openai())
    if r.status_code != 200:
        return ImageGenerateResponse(success=False, provider="openai", error=r.text[:300])
    data = r.json()
    b64 = (data.get("data") or [{}])[0].get("b64_json")
    if not b64:
        return ImageGenerateResponse(success=False, provider="openai", error="No image data returned")
    path = _save_b64(b64, "png", req.output_path)
    return ImageGenerateResponse(success=True, file_path=path, provider="openai")


async def _image_stability(req: ImageGenerateRequest) -> ImageGenerateResponse:
    payload = {
        "prompt": req.prompt,
        "output_format": "png",
        "aspect_ratio": "1:1",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post(
            "https://api.stability.ai/v2beta/stable-image/generate/sd3",
            json=payload,
            headers=_headers_stability(),
        )
    if r.status_code != 200:
        return ImageGenerateResponse(success=False, provider="stability", error=r.text[:300])
    # response is raw PNG bytes when Accept: image/*; JSON otherwise
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        data = r.json()
        b64 = (data.get("artifacts") or [{}])[0].get("base64")
        if not b64:
            return ImageGenerateResponse(success=False, provider="stability", error="No base64 in response")
        path = _save_b64(b64, "png", req.output_path)
    else:
        path = _save_b64(base64.b64encode(r.content).decode(), "png", req.output_path)
    return ImageGenerateResponse(success=True, file_path=path, provider="stability")


async def _image_replicate(req: ImageGenerateRequest) -> ImageGenerateResponse:
    model = req.model or IMAGE_MODELS["replicate"]
    payload = {
        "version": "latest" if "/" not in model else None,
        "input": {
            "prompt": req.prompt,
            "width": req.width,
            "height": req.height,
            "num_inference_steps": req.steps,
            "guidance_scale": req.guidance,
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        r = await c.post(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            json={"input": payload["input"]},
            headers=_headers_replicate(),
        )
        if r.status_code not in (200, 201):
            return ImageGenerateResponse(success=False, provider="replicate", error=r.text[:300])
        pred = r.json()
        pred = await _poll_replicate(pred["id"], c)
    if pred.get("status") != "succeeded":
        return ImageGenerateResponse(success=False, provider="replicate", error=str(pred.get("error", "failed")))
    output = pred.get("output")
    url = output[0] if isinstance(output, list) else output
    if not url:
        return ImageGenerateResponse(success=False, provider="replicate", error="No output URL")
    async with httpx.AsyncClient(timeout=60) as dl:
        img_r = await dl.get(url)
    path = _save_b64(base64.b64encode(img_r.content).decode(), "png", req.output_path)
    return ImageGenerateResponse(success=True, file_path=path, url=url, provider="replicate")


@router.post(
    "/generate/image",
    response_model=ImageGenerateResponse,
    summary="Generate an image using AI (Pro-only)",
)
async def generate_image(
    body: ImageGenerateRequest,
    current_user: User = Depends(get_current_user),
) -> ImageGenerateResponse:
    """
    T-CLI-P12: Generate an image from a text prompt.
    Tries providers in order: fal.ai → OpenAI DALL-E 3 → Stability AI → Replicate.
    Pro accounts only.
    """
    if current_user.plan != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Image generation is a Pro-only feature.",
        )

    _providers: list[tuple[str, Any]] = [
        ("fal", _image_fal),
        ("openai", _image_openai),
        ("stability", _image_stability),
        ("replicate", _image_replicate),
    ]

    # Force a specific provider if requested
    if body.provider:
        _providers = [(n, fn) for n, fn in _providers if n == body.provider]

    last_error: str | None = None
    for name, fn in _providers:
        key_env = {
            "fal": "FAL_KEY",
            "openai": "OPENAI_API_KEY",
            "stability": "STABILITY_API_KEY",
            "replicate": "REPLICATE_API_TOKEN",
        }.get(name, "")
        if not os.environ.get(key_env):
            continue  # skip providers with no key configured
        try:
            result = await fn(body)
            if result.success:
                # T-MEDIA-03: upload to durable storage (MinIO / Cloudinary)
                if result.file_path:
                    durable_url = await _store_media(result.file_path)
                    if durable_url:
                        result = ImageGenerateResponse(
                            success=True,
                            file_path=result.file_path,
                            url=durable_url,
                            provider=result.provider,
                        )
                return result
            last_error = result.error
        except Exception as exc:
            last_error = str(exc)

    return ImageGenerateResponse(success=False, error=last_error or "All providers failed or have no API keys configured")


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------

VIDEO_PROVIDERS = ["fal", "replicate", "runway"]


class VideoGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    image_path: str | None = None     # optional input frame
    output_path: str | None = None
    model: str | None = None
    duration: int = Field(5, ge=2, le=30)
    provider: str | None = None


class VideoGenerateResponse(BaseModel):
    success: bool
    file_path: str | None = None
    url: str | None = None
    provider: str | None = None
    error: str | None = None


async def _video_fal(req: VideoGenerateRequest) -> VideoGenerateResponse:
    model = req.model or "fal-ai/minimax/video-01"
    payload: dict[str, Any] = {"prompt": req.prompt, "duration": req.duration}
    if req.image_path and pathlib.Path(req.image_path).exists():
        raw = pathlib.Path(req.image_path).read_bytes()
        payload["first_frame_image"] = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT * 2) as c:
        r = await c.post(f"https://fal.run/{model}", json=payload, headers=_headers_fal())
    if r.status_code != 200:
        return VideoGenerateResponse(success=False, provider="fal", error=r.text[:300])
    data = r.json()
    url = (data.get("video") or {}).get("url") or (data.get("videos") or [{}])[0].get("url")
    if not url:
        return VideoGenerateResponse(success=False, provider="fal", error="No video URL in response")
    async with httpx.AsyncClient(timeout=120) as dl:
        vid_r = await dl.get(url)
    ext = "mp4"
    out_path = req.output_path or f"/tmp/pakalon-vid-{uuid.uuid4().hex[:8]}.{ext}"
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out_path).write_bytes(vid_r.content)
    return VideoGenerateResponse(success=True, file_path=out_path, url=url, provider="fal")


async def _video_replicate(req: VideoGenerateRequest) -> VideoGenerateResponse:
    model = req.model or "minimax/video-01"
    inp: dict[str, Any] = {"prompt": req.prompt, "num_frames": req.duration * 8}
    if req.image_path and pathlib.Path(req.image_path).exists():
        raw = pathlib.Path(req.image_path).read_bytes()
        inp["first_frame_image"] = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT * 2) as c:
        r = await c.post(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            json={"input": inp},
            headers=_headers_replicate(),
        )
        if r.status_code not in (200, 201):
            return VideoGenerateResponse(success=False, provider="replicate", error=r.text[:300])
        pred = await _poll_replicate(r.json()["id"], c)
    if pred.get("status") != "succeeded":
        return VideoGenerateResponse(success=False, provider="replicate", error=str(pred.get("error", "failed")))
    output = pred.get("output")
    url = output[0] if isinstance(output, list) else output
    async with httpx.AsyncClient(timeout=120) as dl:
        vid_r = await dl.get(url)
    out_path = req.output_path or f"/tmp/pakalon-vid-{uuid.uuid4().hex[:8]}.mp4"
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out_path).write_bytes(vid_r.content)
    return VideoGenerateResponse(success=True, file_path=out_path, url=url, provider="replicate")


async def _video_runway(req: VideoGenerateRequest) -> VideoGenerateResponse:
    if not req.image_path or not pathlib.Path(req.image_path).exists():
        return VideoGenerateResponse(success=False, provider="runway", error="Runway Gen-3 requires an input image")
    raw = pathlib.Path(req.image_path).read_bytes()
    b64 = base64.b64encode(raw).decode()
    payload = {
        "promptImage": f"data:image/png;base64,{b64}",
        "model": req.model or "gen3a_turbo",
        "promptText": req.prompt,
        "duration": req.duration,
        "ratio": "1280:768",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT * 3) as c:
        r = await c.post(
            "https://api.dev.runwayml.com/v1/image_to_video",
            json=payload,
            headers=_headers_runway(),
        )
        if r.status_code not in (200, 201):
            return VideoGenerateResponse(success=False, provider="runway", error=r.text[:300])
        task_id = r.json().get("id")
        # poll
        for _ in range(120):
            await __import__("asyncio").sleep(5)
            poll = await c.get(f"https://api.dev.runwayml.com/v1/tasks/{task_id}", headers=_headers_runway())
            data = poll.json()
            st = data.get("status")
            if st == "SUCCEEDED":
                url = (data.get("output") or [""])[0]
                vid_r = await c.get(url)
                out_path = req.output_path or f"/tmp/pakalon-vid-{uuid.uuid4().hex[:8]}.mp4"
                pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(out_path).write_bytes(vid_r.content)
                return VideoGenerateResponse(success=True, file_path=out_path, url=url, provider="runway")
            if st in ("FAILED", "CANCELED"):
                return VideoGenerateResponse(success=False, provider="runway", error=str(data.get("failure", "failed")))
    return VideoGenerateResponse(success=False, provider="runway", error="Runway task timed out")


@router.post(
    "/generate/video",
    response_model=VideoGenerateResponse,
    summary="Generate a video using AI (Pro-only)",
)
async def generate_video(
    body: VideoGenerateRequest,
    current_user: User = Depends(get_current_user),
) -> VideoGenerateResponse:
    """
    T-CLI-P12: Generate a video from a text prompt (optionally with an input image).
    Tries providers in order: fal.ai → Replicate → Runway Gen-3.
    Pro accounts only.
    """
    if current_user.plan != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Video generation is a Pro-only feature.",
        )

    _providers: list[tuple[str, Any]] = [
        ("fal", _video_fal),
        ("replicate", _video_replicate),
        ("runway", _video_runway),
    ]

    if body.provider:
        _providers = [(n, fn) for n, fn in _providers if n == body.provider]

    last_error: str | None = None
    for name, fn in _providers:
        key_env = {
            "fal": "FAL_KEY",
            "replicate": "REPLICATE_API_TOKEN",
            "runway": "RUNWAYML_API_SECRET",
        }.get(name, "")
        if not os.environ.get(key_env):
            continue
        try:
            result = await fn(body)
            if result.success:
                # T-MEDIA-03: upload to durable storage (MinIO / Cloudinary)
                if result.file_path:
                    durable_url = await _store_media(result.file_path)
                    if durable_url:
                        result = VideoGenerateResponse(
                            success=True,
                            file_path=result.file_path,
                            url=durable_url,
                            provider=result.provider,
                        )
                return result
            last_error = result.error
        except Exception as exc:
            last_error = str(exc)

    return VideoGenerateResponse(success=False, error=last_error or "All providers failed or have no API keys configured")
