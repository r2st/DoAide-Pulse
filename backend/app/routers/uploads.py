"""Image upload and serving for cover images and marketing galleries."""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings
from app.deps import get_current_user
from app.models.user import User
from app.ratelimit import account_key, limiter
from app.schemas.errors import AUTHENTICATED, errors

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["uploads"])

UPLOAD_LIMIT = "120/hour"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


class UploadOut(BaseModel):
    url: str
    filename: str
    size: int


def _upload_dir() -> FilePath:
    base = FilePath(settings.upload_dir)
    if not base.is_absolute():
        base = FilePath(__file__).resolve().parent.parent.parent / base
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
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an image",
    responses=errors(
        status.HTTP_400_BAD_REQUEST,
        *AUTHENTICATED,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(UPLOAD_LIMIT, key_func=account_key)
async def upload_image(
    request: Request,
    response: Response,
    file: UploadFile,
    user: User = Depends(get_current_user),
) -> UploadOut:
    """Accept an image file and persist it to the upload directory."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {file.content_type}. "
            f"Allowed: jpg, png, webp, gif",
        )

    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File too large (max {settings.max_upload_bytes // (1024 * 1024)} MB)",
        )

    ext = _ext_from_content_type(file.content_type)
    file_hash = hashlib.sha256(data).hexdigest()[:12]
    filename = f"{uuid.uuid4().hex[:8]}_{file_hash}{ext}"

    dest = _upload_dir() / filename
    dest.write_bytes(data)

    url = f"/api/v1/uploads/{filename}"
    return UploadOut(url=url, filename=filename, size=len(data))


@router.get(
    "/{filename}",
    summary="Serve an uploaded image",
    responses=errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
@limiter.limit(settings.rate_limit_public_read)
async def serve_image(
    request: Request,
    response: Response,
    filename: str = Path(max_length=255),
) -> FileResponse:
    """Return a previously uploaded image by filename."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename"
        )

    path = _upload_dir() / filename
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        )

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
