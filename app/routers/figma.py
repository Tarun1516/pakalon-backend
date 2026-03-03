"""Figma API router.

Endpoints:
  PUT  /users/me/figma-pat              — Store/update the user's Figma PAT
  DELETE /users/me/figma-pat            — Remove the stored Figma PAT
  GET  /figma/file/{file_key}           — Fetch full Figma file document
  GET  /figma/file/{file_key}/frames    — List all FRAME nodes (screens)
  GET  /figma/file/{file_key}/export    — Export nodes as SVG/PNG/PDF
  GET  /figma/file/{file_key}/images    — Fetch image fill assets
  GET  /figma/file/{file_key}/comments  — Fetch design comments

Pro plan required: all /figma/* endpoints.
Free plan: PAT management allowed; file access returns 402.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.users import FigmaPatRequest
from app import services

router = APIRouter(tags=["figma"])


def _require_figma_pat(user: User) -> str:
    """Return the user's Figma PAT or raise 400 if not set."""
    if not user.figma_pat:
        raise HTTPException(
            status_code=400,
            detail="No Figma Personal Access Token configured. Use PUT /users/me/figma-pat to add one.",
        )
    return user.figma_pat


def _require_pro(user: User) -> None:
    """Restrict endpoint to pro-plan users."""
    if user.plan not in ("pro", "enterprise"):
        raise HTTPException(
            status_code=402,
            detail="Figma file access requires a Pro subscription.",
        )


# ── PAT management (available to all plans) ───────────────────────

@router.put("/users/me/figma-pat", summary="Store Figma Personal Access Token")
async def set_figma_pat(
    body: FigmaPatRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Validate and store a Figma PAT against the authenticated user record."""
    from app.services.figma import validate_pat  # noqa: PLC0415
    if not await validate_pat(body.pat):
        raise HTTPException(status_code=400, detail="Invalid Figma Personal Access Token.")
    user.figma_pat = body.pat
    db.add(user)
    await db.commit()
    return {"status": "ok", "message": "Figma PAT saved."}


@router.delete("/users/me/figma-pat", summary="Remove Figma Personal Access Token")
async def delete_figma_pat(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Clear the stored Figma PAT for the authenticated user."""
    user.figma_pat = None
    db.add(user)
    await db.commit()
    return {"status": "ok", "message": "Figma PAT removed."}


# ── File access (pro plan required) ───────────────────────────────

@router.get("/figma/file/{file_key}", summary="Get Figma file document")
async def get_figma_file(
    file_key: str,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """
    Fetch the Figma document tree for the given file key (depth=2).
    Requires a Figma PAT stored on the user account and a Pro subscription.
    """
    _require_pro(user)
    pat = _require_figma_pat(user)
    try:
        from app.services.figma import get_file  # noqa: PLC0415
        return await get_file(pat, file_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Figma API error: {exc}") from exc


@router.get("/figma/file/{file_key}/frames", summary="List FRAME nodes in Figma file")
async def get_figma_frames(
    file_key: str,
    user: Annotated[User, Depends(get_current_user)],
) -> list[dict[str, Any]]:
    """
    Return all top-level FRAME nodes (screens) across all pages in the file.
    Each frame entry includes id, name, page, width, and height.
    """
    _require_pro(user)
    pat = _require_figma_pat(user)
    try:
        from app.services.figma import get_frames  # noqa: PLC0415
        return await get_frames(pat, file_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Figma API error: {exc}") from exc


@router.get("/figma/file/{file_key}/export", summary="Export nodes from Figma file")
async def export_figma_nodes(
    file_key: str,
    node_ids: Annotated[list[str], Query(description="Comma-separated Figma node IDs")],
    fmt: Annotated[str, Query(description="Export format: svg | png | pdf | jpg")] = "svg",
    scale: Annotated[float, Query(description="Export scale (1.0–4.0)", ge=0.5, le=4.0)] = 1.0,
    user: Annotated[User, Depends(get_current_user)] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Export one or more Figma nodes as SVG/PNG/PDF/JPG.
    Returns a mapping of node_id → CDN URL.
    """
    _require_pro(user)
    pat = _require_figma_pat(user)
    if not node_ids:
        raise HTTPException(status_code=400, detail="At least one node_id is required.")
    try:
        from app.services.figma import export_nodes  # noqa: PLC0415
        urls = await export_nodes(pat, file_key, node_ids, fmt=fmt, scale=scale)
        return {"file_key": file_key, "format": fmt, "images": urls}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Figma API error: {exc}") from exc


@router.get("/figma/file/{file_key}/images", summary="Get image fills from Figma file")
async def get_figma_image_fills(
    file_key: str,
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Retrieve all raster image fills embedded in the Figma document."""
    _require_pro(user)
    pat = _require_figma_pat(user)
    try:
        from app.services.figma import get_image_fills  # noqa: PLC0415
        return await get_image_fills(pat, file_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Figma API error: {exc}") from exc


@router.get("/figma/file/{file_key}/comments", summary="Get comments from Figma file")
async def get_figma_comments(
    file_key: str,
    user: Annotated[User, Depends(get_current_user)],
) -> list[dict[str, Any]]:
    """Retrieve all design review comments posted on the Figma file."""
    _require_pro(user)
    pat = _require_figma_pat(user)
    try:
        from app.services.figma import get_comments  # noqa: PLC0415
        return await get_comments(pat, file_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Figma API error: {exc}") from exc
