"""Figma REST API service.

Wraps the Figma Web API (https://www.figma.com/developers/api) to allow
the CLI to fetch design files, extract frame metadata, and export SVG/PNG
assets — powering the Figma-to-code pipeline.

All methods require a valid Figma Personal Access Token (PAT) stored on the
user's record (`users.figma_pat`).
"""
from __future__ import annotations

from typing import Any

import httpx

FIGMA_BASE_URL = "https://api.figma.com/v1"


def _headers(pat: str) -> dict[str, str]:
    return {"X-Figma-Token": pat}


async def get_file(pat: str, file_key: str) -> dict[str, Any]:
    """
    GET /v1/files/:file_key
    Returns the full document tree for a Figma file.
    Only the `document` node is returned to avoid huge payloads.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FIGMA_BASE_URL}/files/{file_key}",
            headers=_headers(pat),
            params={"depth": 2},
        )
        resp.raise_for_status()
        data = resp.json()
    return {
        "file_key": file_key,
        "name": data.get("name"),
        "last_modified": data.get("lastModified"),
        "thumbnail_url": data.get("thumbnailUrl"),
        "version": data.get("version"),
        "document": data.get("document"),
    }


async def get_frames(pat: str, file_key: str) -> list[dict[str, Any]]:
    """
    GET /v1/files/:file_key  (depth=2)
    Extracts all top-level FRAME nodes from the Figma canvas pages —
    these correspond to individual screens or components.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FIGMA_BASE_URL}/files/{file_key}",
            headers=_headers(pat),
            params={"depth": 2},
        )
        resp.raise_for_status()
        data = resp.json()

    frames: list[dict[str, Any]] = []
    document = data.get("document", {})
    for page in document.get("children", []):
        for node in page.get("children", []):
            if node.get("type") == "FRAME":
                frames.append({
                    "id": node["id"],
                    "name": node.get("name", ""),
                    "page": page.get("name", ""),
                    "width": node.get("absoluteBoundingBox", {}).get("width"),
                    "height": node.get("absoluteBoundingBox", {}).get("height"),
                })
    return frames


async def export_nodes(
    pat: str,
    file_key: str,
    node_ids: list[str],
    fmt: str = "svg",
    scale: float = 1.0,
) -> dict[str, Any]:
    """
    GET /v1/images/:file_key
    Exports one or more nodes as SVG/PNG/PDF/JPG.
    Returns a mapping of node_id → CDN URL.
    """
    if fmt not in ("svg", "png", "pdf", "jpg"):
        raise ValueError(f"Unsupported export format: {fmt}")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{FIGMA_BASE_URL}/images/{file_key}",
            headers=_headers(pat),
            params={
                "ids": ",".join(node_ids),
                "format": fmt,
                "scale": str(scale),
            },
        )
        resp.raise_for_status()
        data = resp.json()
    if data.get("err"):
        raise RuntimeError(f"Figma export error: {data['err']}")
    return data.get("images", {})


async def get_image_fills(pat: str, file_key: str) -> dict[str, Any]:
    """
    GET /v1/files/:file_key/images
    Returns all image fills (raster assets embedded in the document).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FIGMA_BASE_URL}/files/{file_key}/images",
            headers=_headers(pat),
        )
        resp.raise_for_status()
        return resp.json()


async def get_comments(pat: str, file_key: str) -> list[dict[str, Any]]:
    """
    GET /v1/files/:file_key/comments
    Returns all comments on the file (useful for design feedback integration).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FIGMA_BASE_URL}/files/{file_key}/comments",
            headers=_headers(pat),
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("comments", [])


async def validate_pat(pat: str) -> bool:
    """
    GET /v1/me — verify the PAT is valid by fetching the authenticated user.
    Returns True if the token is valid, False otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{FIGMA_BASE_URL}/me", headers=_headers(pat))
        return resp.status_code == 200
    except Exception:
        return False
