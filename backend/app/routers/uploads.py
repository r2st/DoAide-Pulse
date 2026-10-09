"""Image upload and serving for cover images and marketing galleries."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.errors import AUTHENTICATED, errors

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["uploads"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


def _upload_dir() -> Path:
    base = Path(settings.upload_dir)
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent.parent / base
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ext_from_content_type(ct: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct, "")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Upload an image",
    responses=errors(status.HTTP_400_BAD_REQUEST, *AUTHENTICATED),
)
async def upload_image(
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. "
            f"Allowed: jpg, png, webp, gif",
        )

    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File too large (max {settings.max_upload_bytes // (1024 * 1024)} MB)",
        )

    ext = _ext_from_content_type(file.content_type)
    file_hash = hashlib.sha256(data).hexdigest()[:12]
    filename = f"{uuid.uuid4().hex[:8]}_{file_hash}{ext}"

    dest = _upload_dir() / filename
    dest.write_bytes(data)

    url = f"/api/v1/uploads/{filename}"
    return {"url": url, "filename": filename, "size": len(data)}


@router.get(
    "/{filename}",
    summary="Serve an uploaded image",
    responses=errors(status.HTTP_404_NOT_FOUND),
)
async def serve_image(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = _upload_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    ext = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
