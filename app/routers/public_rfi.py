"""Scoped, revocable links for external RFI respondents."""

import asyncio
import hashlib
import re
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.models import utcnow
from app.security import require_permission
from app.services.drive import DriveStorage
from app.routers.receipts import ALLOWED_TYPES, _safe_filename

router = APIRouter(tags=["public RFI"])
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def _shared_rfi(token: str):
    if not TOKEN_RE.fullmatch(token):
        raise HTTPException(404, "RFI link is unavailable")
    db = get_database()
    link = await db.rfi_share_links.find_one({"token_hash": _hash(token), "revoked_at": None,
                                              "expires_at": {"$gt": utcnow()}})
    if link is None:
        raise HTTPException(404, "RFI link is unavailable")
    rfi = await db.receipts.find_one({"_id": link["rfi_id"], "document_type": "rfi", "deleted_at": None})
    if rfi is None:
        raise HTTPException(404, "RFI link is unavailable")
    return link, rfi


@router.post("/receipts/{rfi_id}/share-link", status_code=201)
async def create_rfi_share_link(rfi_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    db = get_database()
    rfi = await db.receipts.find_one({"_id": rfi_id, "document_type": "rfi", "deleted_at": None})
    if rfi is None:
        raise HTTPException(404, "RFI not found")
    await db.rfi_share_links.update_many({"rfi_id": rfi_id, "revoked_at": None},
                                         {"$set": {"revoked_at": utcnow()}})
    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=30)
    await db.rfi_share_links.insert_one({"_id": uuid4(), "rfi_id": rfi_id, "token_hash": _hash(token),
                                        "created_by": actor["_id"], "created_at": utcnow(),
                                        "expires_at": expires_at, "revoked_at": None})
    await write_audit("RFI_SHARE_LINK_CREATED", actor["_id"], "rfi", rfi_id)
    return {"token": token, "expires_at": expires_at}


@router.delete("/receipts/{rfi_id}/share-link")
async def revoke_rfi_share_link(rfi_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    result = await get_database().rfi_share_links.update_many({"rfi_id": rfi_id, "revoked_at": None},
                                                                {"$set": {"revoked_at": utcnow()}})
    await write_audit("RFI_SHARE_LINK_REVOKED", actor["_id"], "rfi", rfi_id)
    return {"success": True, "revoked": result.modified_count}


@router.get("/public/rfi")
async def get_public_rfi(token: str = Header(..., alias="X-RFI-Token")):
    _, rfi = await _shared_rfi(token)
    attachments = []
    async for item in get_database().rfi_attachments.find({"rfi_id": rfi["_id"], "deleted_at": None}):
        attachments.append({"id": str(item["_id"]), "filename": item["filename"],
                            "mime_type": item["mime_type"]})
    return {"id": str(rfi["_id"]), "rfi_number": rfi.get("rfi_number"),
            "rfi_subject": rfi.get("rfi_subject"), "rfi_question": rfi.get("rfi_question"),
            "rfi_to": rfi.get("rfi_to"), "rfi_attention_name": rfi.get("rfi_attention_name"),
            "rfi_due_at": rfi.get("rfi_due_at"), "project": rfi.get("link_label"),
            "attachments": attachments}


@router.get("/public/rfi/attachments/{attachment_id}/file")
async def get_public_rfi_attachment(attachment_id: UUID, token: str = Header(..., alias="X-RFI-Token")):
    _, rfi = await _shared_rfi(token)
    item = await get_database().rfi_attachments.find_one({"_id": attachment_id, "rfi_id": rfi["_id"],
                                                          "deleted_at": None})
    if item is None:
        raise HTTPException(404, "Attachment not found")
    if item["storage_provider"] == "google_drive":
        settings = get_settings()
        drive = DriveStorage(settings.google_drive_credentials_json, settings.google_drive_root_folder_id)
        content = await asyncio.to_thread(drive.download, item["file_id"])
        return Response(content, media_type=item["mime_type"],
                        headers={"Content-Disposition": f'inline; filename="{_safe_filename(item["filename"])}"'})
    path = Path(item["local_path"])
    if not path.is_file():
        raise HTTPException(404, "Attachment not found")
    return FileResponse(path, media_type=item["mime_type"], filename=item["filename"],
                        content_disposition_type="inline")


@router.post("/public/rfi/responses", status_code=201)
async def submit_public_rfi_response(token: str = Header(..., alias="X-RFI-Token"), responder_name: str = Form(...),
                                     response_text: str = Form(""), file: UploadFile | None = File(None)):
    link, rfi = await _shared_rfi(token)
    name, answer = responder_name.strip(), response_text.strip()
    if not name or len(name) > 200 or len(answer) > 10000 or (not answer and file is None):
        raise HTTPException(422, "Provide your name and an answer or attachment")
    attachment = {}
    if file is not None:
        settings = get_settings()
        if file.content_type not in ALLOWED_TYPES:
            raise HTTPException(415, "Upload a JPEG, PNG, WebP, or PDF file")
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "File is larger than the configured upload limit")
        filename = _safe_filename(file.filename or "response")
        year = str(rfi["incurred_at"].year)
        category = rfi.get("category") or "other"
        if settings.google_drive_credentials_json and settings.google_drive_root_folder_id:
            drive = DriveStorage(settings.google_drive_credentials_json, settings.google_drive_root_folder_id)
            storage = await asyncio.to_thread(drive.upload, content, filename, file.content_type,
                                               ["RFI", year, category.replace("_", " ").title()])
            provider = "google_drive"
        else:
            target = Path(settings.local_upload_directory) / "RFI" / year / category
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{uuid4()}-{filename}"
            await asyncio.to_thread(path.write_bytes, content)
            storage = {"local_path": str(path)}
            provider = "local"
        attachment = {"filename": filename, "mime_type": file.content_type,
                      "size_bytes": len(content), "storage_provider": provider, **storage}
    response = {"_id": uuid4(), "rfi_id": rfi["_id"], "response_text": answer,
                "responder_name": name, "responded_at": utcnow(), "created_at": utcnow(),
                "created_by": None, "share_link_id": link["_id"], "deleted_at": None, **attachment}
    await get_database().rfi_responses.insert_one(response)
    await get_database().receipts.update_one(
        {"_id": rfi["_id"], "document_type": "rfi"},
        {"$set": {"status": "closed", "closed_at": utcnow(), "updated_at": utcnow()}}
    )
    await write_audit("RFI_EXTERNAL_RESPONSE_ADDED", None, "rfi_response", response["_id"],
                      metadata={"rfi_id": str(rfi["_id"]), "share_link_id": str(link["_id"])})
    return {"success": True}
