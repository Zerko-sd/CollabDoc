"""Images embedded in pages.

The upload is the raw file as the request body, typed by its Content-Type, so
the browser can send a pasted or dropped image with a plain ``fetch`` and the
server needs no multipart parser. Only raster images are accepted, and the
bytes must match the declared type: SVG is refused because it can carry
script, and the signature check stops an HTML file posing as a PNG.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import DocumentAccess, get_current_user, require_editor, resolve_access
from ..models import Attachment, User
from ..schemas import AttachmentOut
from ..storage import get_storage

router = APIRouter(prefix="/api", tags=["files"])

# Accepted types, their file extension, and the bytes each must start with.
IMAGE_TYPES = {
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/gif": (".gif", (b"GIF87a", b"GIF89a")),
    "image/webp": (".webp", (b"RIFF",)),
}


def attachment_out(row: Attachment) -> AttachmentOut:
    return AttachmentOut(
        id=row.id, url="/api/files/%s" % row.id, filename=row.filename, mime=row.mime, size=row.size
    )


def serve(row: Attachment) -> Response:
    data = get_storage().get(row.path)
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(
        data,
        media_type=row.mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.post(
    "/documents/{document_id}/files",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    request: Request,
    filename: str = Query(default="image", max_length=255),
    access: DocumentAccess = Depends(require_editor),
    db: Session = Depends(get_db),
) -> AttachmentOut:
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in IMAGE_TYPES:
        raise HTTPException(
            status_code=415, detail="Only PNG, JPEG, GIF and WebP images can be uploaded"
        )
    limit = get_settings().upload_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="Images are limited to %d MB" % (limit // 2**20))

    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(
                status_code=413, detail="Images are limited to %d MB" % (limit // 2**20)
            )
    if not data:
        raise HTTPException(status_code=422, detail="The upload was empty")
    extension, signatures = IMAGE_TYPES[mime]
    if not any(bytes(data).startswith(signature) for signature in signatures) or (
        mime == "image/webp" and data[8:12] != b"WEBP"
    ):
        raise HTTPException(status_code=415, detail="The file is not a valid %s image" % mime)

    row = Attachment(
        document_id=access.document.id,
        uploader_id=access.user.id,
        filename=Path(filename).name[:255] or "image",
        mime=mime,
        size=len(data),
        path="",
    )
    db.add(row)
    db.flush()
    row.path = row.id + extension
    get_storage().put(row.path, bytes(data), mime)
    db.commit()
    return attachment_out(row)


@router.get("/files/{file_id}", response_class=Response)
def get_file(
    file_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    row = db.get(Attachment, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    # Readable by anyone who can open the page it belongs to.
    resolve_access(db, row.document_id, user)
    return serve(row)
