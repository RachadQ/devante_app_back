import asyncio
import re
from contextlib import suppress
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from starlette.datastructures import Headers

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.models import serialize, utcnow
from app.security import require_permission
from app.services.drive import DriveStorage
from app.services.ocr import extract_text, suggested_total, suggested_vendor
from app.services.receipt_jobs import enqueue_preview_job, process_preview_job_by_id

router = APIRouter(prefix="/receipts", tags=["receipts"])
settings = get_settings()
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
CATEGORIES = {"gas", "client_meals", "maintenance", "job_expense", "other"}


class ReceiptUpdate(BaseModel):
    vendor: str | None = Field(default=None, max_length=160)
    amount: float | None = Field(default=None, ge=0)
    category: str | None = None
    transaction_type: str | None = None
    incurred_at: date | None = None
    link_type: str | None = None
    link_id: str | None = Field(default=None, max_length=160)
    link_label: str | None = Field(default=None, max_length=200)
    vehicle_id: UUID | None = None
    fuel_litres: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False)


@router.post("/preview", status_code=202)
async def preview_receipt_ocr(
    file: UploadFile = File(...), document_type: str = Form("receipt"),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    """Queue OCR and return immediately; temporary bytes are removed after processing."""
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP, or PDF file")
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "File is larger than the configured upload limit")
    if document_type not in {"receipt", "rfi"}:
        raise HTTPException(422, "Document type must be receipt or rfi")
    db = get_database()
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    job_id = uuid4()
    filename = _safe_filename(file.filename or "upload")
    try:
        now = utcnow()
        expires_at = now + timedelta(hours=1)
        file_id = await bucket.upload_from_stream(
            filename, content, metadata={"job_id": str(job_id), "mime_type": file.content_type,
                                         "expires_at": expires_at},
        )
        await db.ocr_preview_jobs.insert_one({
            "_id": job_id, "status": "queued", "gridfs_file_id": file_id,
            "filename": filename, "mime_type": file.content_type, "size_bytes": len(content),
            "document_type": document_type,
            "created_by": actor["_id"], "created_at": now, "updated_at": now,
            "expires_at": expires_at,
        })
    except Exception:
        if "file_id" in locals():
            with suppress(Exception):
                await bucket.delete(file_id)
        raise
    await enqueue_preview_job(job_id)
    return {"job_id": str(job_id), "status": "queued"}


@router.get("/preview-jobs")
async def list_receipt_preview_jobs(actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    query = {"created_by": actor["_id"], "expires_at": {"$gt": utcnow()}}
    jobs = []
    async for job in get_database().ocr_preview_jobs.find(query).sort("created_at", -1):
        item = {
            "job_id": str(job["_id"]), "status": job["status"],
            "filename": job["filename"], "documentType": job.get("document_type", "receipt"),
            "mime_type": job["mime_type"], "created_at": job["created_at"],
        }
        if job["status"] == "completed":
            item["preview"] = job.get("result", {})
        elif job["status"] == "failed":
            item["error"] = job.get("error", "OCR preview failed")
        jobs.append(item)
    return jobs


@router.get("/preview/{job_id}")
async def get_receipt_preview_job(
    job_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    db = get_database()
    job = await db.ocr_preview_jobs.find_one({"_id": job_id, "created_by": actor["_id"]})
    if not job:
        raise HTTPException(404, "Receipt preview job not found or expired")
    if job["status"] == "queued":
        processed = await process_preview_job_by_id(job_id)
        if processed:
            job = processed
    response: dict = {"job_id": str(job_id), "status": job["status"],
                      "filename": job["filename"], "mime_type": job["mime_type"],
                      "documentType": job.get("document_type", "receipt")}
    if job["status"] == "completed":
        response["preview"] = job.get("result", {})
    elif job["status"] == "failed":
        response["error"] = job.get("error", "OCR preview failed")
    return response


@router.get("/preview/{job_id}/file")
async def get_receipt_preview_file(
    job_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    job = await get_database().ocr_preview_jobs.find_one({"_id": job_id, "created_by": actor["_id"]})
    if not job:
        raise HTTPException(404, "Receipt preview job not found or expired")
    bucket = AsyncIOMotorGridFSBucket(get_database(), bucket_name="ocr_preview_uploads")
    try:
        stream = await bucket.open_download_stream(job["gridfs_file_id"])
        content = await stream.read()
    except Exception as exc:
        raise HTTPException(404, "Temporary receipt file is no longer available") from exc
    return Response(content, media_type=job["mime_type"], headers={
        "Content-Disposition": f'inline; filename="{job["filename"]}"',
        "Cache-Control": "private, no-store",
    })


def _validate(category: str, document_type: str, transaction_type: str) -> None:
    if category not in CATEGORIES:
        raise HTTPException(422, f"Category must be one of: {', '.join(sorted(CATEGORIES))}")
    if document_type not in {"receipt", "rfi"}:
        raise HTTPException(422, "Document type must be receipt or rfi")
    if transaction_type not in {"expense", "income"}:
        raise HTTPException(422, "Transaction type must be expense or income")


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:180] or "upload"


async def _validate_job_link(link_type: str | None, link_id: str | None) -> str | None:
    if link_type != "job":
        return link_id
    if not link_id:
        raise HTTPException(422, "Select a job for this document")
    job = await get_database().jobs.find_one({"code": link_id.upper(), "deleted_at": None})
    if job is None:
        try:
            job = await get_database().jobs.find_one({"_id": UUID(link_id), "deleted_at": None})
        except ValueError:
            pass
    if job is None:
        raise HTTPException(422, "Job not found")
    return job["code"]


@router.post("", status_code=201)
async def upload_receipt(
    file: UploadFile = File(...), category: str = Form("other"), document_type: str = Form("receipt"),
    transaction_type: str = Form("expense"), incurred_at: date = Form(default_factory=date.today),
    amount: float | None = Form(None), vendor: str | None = Form(None), currency: str = Form("CAD"),
    link_type: str | None = Form(None), link_id: str | None = Form(None), link_label: str | None = Form(None),
    vehicle_id: UUID | None = Form(None), fuel_litres: float | None = Form(None),
    ocr_text_override: str | None = Form(None),
    rfi_number: str | None = Form(None), rfi_subject: str | None = Form(None),
    rfi_question: str | None = Form(None), rfi_to: str | None = Form(None),
    rfi_attention_name: str | None = Form(None), rfi_attention_phone: str | None = Form(None),
    rfi_attention_email: str | None = Form(None),
    rfi_due_at: date | None = Form(None),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    _validate(category, document_type, transaction_type)
    if document_type == "rfi":
        for value, limit, label in ((rfi_number, 80, "RFI number"), (rfi_subject, 200, "RFI subject"),
                                    (rfi_question, 5000, "RFI question"), (rfi_to, 1000, "RFI recipient"),
                                    (rfi_attention_name, 200, "Attention name"),
                                    (rfi_attention_phone, 80, "Attention phone"),
                                    (rfi_attention_email, 200, "Attention email")):
            if value is not None and len(value) > limit:
                raise HTTPException(422, f"{label} is too long")
    link_id = await _validate_job_link(link_type, link_id)
    if vehicle_id is not None:
        if document_type != "receipt" or category != "gas":
            raise HTTPException(422, "A vehicle can only be assigned to a gas receipt")
        if await get_database().vehicles.find_one({"_id": vehicle_id, "deleted_at": None}) is None:
            raise HTTPException(422, "Vehicle not found")
    if fuel_litres is not None and (vehicle_id is None or category != "gas"):
        raise HTTPException(422, "Fuel litres require a vehicle and gas category")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP, or PDF file")
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "File is larger than the configured upload limit")
    filename = _safe_filename(file.filename or "upload")
    ocr_text = ocr_text_override.strip() if ocr_text_override is not None else ""
    if ocr_text_override is None and file.content_type.startswith("image/"):
        try:
            ocr_text = await asyncio.to_thread(extract_text, content)
        except Exception:
            ocr_text = ""
    detected_amount = suggested_total(ocr_text)
    detected_vendor = suggested_vendor(ocr_text)
    year = str(incurred_at.year)
    drive_data: dict[str, str] = {}
    storage_provider = "local"
    if settings.google_drive_credentials_json and settings.google_drive_root_folder_id:
        drive = DriveStorage(settings.google_drive_credentials_json, settings.google_drive_root_folder_id)
        folders = ["RFI" if document_type == "rfi" else "Receipts", year, category.replace("_", " ").title()]
        drive_data = await asyncio.to_thread(drive.upload, content, filename, file.content_type, folders)
        storage_provider = "google_drive"
    else:
        target = Path(settings.local_upload_directory) / ("RFI" if document_type == "rfi" else "Receipts") / year / category
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{uuid4()}-{filename}"
        await asyncio.to_thread(path.write_bytes, content)
        drive_data = {"local_path": str(path)}
    now = utcnow()
    document = {
        "_id": uuid4(), "document_type": document_type, "transaction_type": transaction_type,
        "category": category, "vendor": vendor or detected_vendor, "amount": amount if amount is not None else detected_amount,
        "currency": currency.upper()[:3], "incurred_at": datetime.combine(incurred_at, time.min, tzinfo=timezone.utc),
        "filename": filename, "mime_type": file.content_type, "size_bytes": len(content), "ocr_text": ocr_text,
        "link_type": link_type or None, "link_id": link_id or None, "link_label": link_label or None,
        "vehicle_id": vehicle_id, "fuel_litres": fuel_litres,
        "rfi_number": rfi_number.strip() if document_type == "rfi" and rfi_number else None,
        "rfi_subject": rfi_subject.strip() if document_type == "rfi" and rfi_subject else None,
        "rfi_question": rfi_question.strip() if document_type == "rfi" and rfi_question else None,
        "rfi_to": rfi_to.strip() if document_type == "rfi" and rfi_to else None,
        "rfi_attention_name": rfi_attention_name.strip() if document_type == "rfi" and rfi_attention_name else None,
        "rfi_attention_phone": rfi_attention_phone.strip() if document_type == "rfi" and rfi_attention_phone else None,
        "rfi_attention_email": rfi_attention_email.strip() if document_type == "rfi" and rfi_attention_email else None,
        "rfi_due_at": datetime.combine(rfi_due_at, time.min, tzinfo=timezone.utc) if document_type == "rfi" and rfi_due_at else None,
        "storage_provider": storage_provider, **drive_data, "created_by": actor["_id"], "created_at": now,
        "updated_at": now, "deleted_at": None,
    }
    await get_database().receipts.insert_one(document)
    await write_audit("RECEIPT_UPLOADED" if document_type == "receipt" else "RFI_UPLOADED", actor["_id"], document_type, document["_id"])
    return serialize(document)


@router.post("/from-preview/{job_id}", status_code=201)
async def upload_receipt_from_preview(
    job_id: UUID, category: str = Form("other"), document_type: str = Form("receipt"),
    transaction_type: str = Form("expense"), incurred_at: date = Form(default_factory=date.today),
    amount: float | None = Form(None), vendor: str | None = Form(None), currency: str = Form("CAD"),
    link_type: str | None = Form(None), link_id: str | None = Form(None), link_label: str | None = Form(None),
    vehicle_id: UUID | None = Form(None), fuel_litres: float | None = Form(None),
    ocr_text_override: str | None = Form(None),
    rfi_number: str | None = Form(None), rfi_subject: str | None = Form(None),
    rfi_question: str | None = Form(None), rfi_to: str | None = Form(None),
    rfi_attention_name: str | None = Form(None), rfi_attention_phone: str | None = Form(None),
    rfi_attention_email: str | None = Form(None),
    rfi_due_at: date | None = Form(None),
    actor: dict = Depends(require_permission("RECEIPTS_CREATE")),
):
    db = get_database()
    job = await db.ocr_preview_jobs.find_one_and_update(
        {"_id": job_id, "created_by": actor["_id"], "status": {"$in": ["completed", "failed"]}},
        {"$set": {"status": "confirming", "updated_at": utcnow()}},
        return_document=ReturnDocument.BEFORE,
    )
    if not job:
        raise HTTPException(409, "Receipt preview is not ready, expired, or already being confirmed")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="ocr_preview_uploads")
    try:
        stream = await bucket.open_download_stream(job["gridfs_file_id"])
        content = await stream.read()
        reviewed_text = ocr_text_override
        if reviewed_text is None:
            reviewed_text = str(job.get("result", {}).get("ocr_text", ""))
        upload = UploadFile(
            file=BytesIO(content), filename=job["filename"], size=len(content),
            headers=Headers({"content-type": job["mime_type"]}),
        )
        saved = await upload_receipt(
            file=upload, category=category, document_type=document_type,
            transaction_type=transaction_type, incurred_at=incurred_at, amount=amount,
            vendor=vendor, currency=currency, link_type=link_type, link_id=link_id,
            link_label=link_label, vehicle_id=vehicle_id, fuel_litres=fuel_litres,
            ocr_text_override=reviewed_text, rfi_number=rfi_number,
            rfi_subject=rfi_subject, rfi_question=rfi_question, rfi_to=rfi_to,
            rfi_attention_name=rfi_attention_name, rfi_attention_phone=rfi_attention_phone,
            rfi_attention_email=rfi_attention_email,
            rfi_due_at=rfi_due_at, actor=actor,
        )
    except Exception:
        await db.ocr_preview_jobs.update_one(
            {"_id": job_id, "status": "confirming"},
            {"$set": {"status": job["status"], "updated_at": utcnow()}},
        )
        raise
    with suppress(Exception):
        await bucket.delete(job["gridfs_file_id"])
    await db.ocr_preview_jobs.delete_one({"_id": job_id})
    return saved


@router.get("")
async def list_receipts(year: int | None = None, category: str | None = None, document_type: str | None = None,
                        _: dict = Depends(require_permission("RECEIPTS_READ"))):
    query: dict = {"deleted_at": None}
    if year:
        query["incurred_at"] = {"$gte": datetime(year, 1, 1, tzinfo=timezone.utc), "$lt": datetime(year + 1, 1, 1, tzinfo=timezone.utc)}
    if category:
        query["category"] = category
    if document_type:
        query["document_type"] = document_type
    return [serialize(item) async for item in get_database().receipts.find(query).sort("incurred_at", -1)]


@router.get("/{receipt_id}/file")
async def download_receipt_file(receipt_id: UUID, _: dict = Depends(require_permission("RECEIPTS_READ"))):
    document = await get_database().receipts.find_one({"_id": receipt_id, "deleted_at": None})
    if document is None:
        raise HTTPException(404, "Document not found")
    if not document.get("mime_type"):
        raise HTTPException(404, "This RFI has no original file")
    if document["storage_provider"] == "google_drive":
        return {"web_url": document["web_url"]}
    path = Path(document["local_path"])
    if not path.is_file():
        raise HTTPException(404, "File is no longer available")
    return FileResponse(path, media_type=document["mime_type"], filename=document["filename"],
                        content_disposition_type="inline")


async def _rfi_or_404(rfi_id: UUID) -> dict:
    rfi = await get_database().receipts.find_one({"_id": rfi_id, "document_type": "rfi", "deleted_at": None})
    if rfi is None:
        raise HTTPException(404, "RFI not found")
    return rfi


def _public_rfi_response(document: dict) -> dict:
    item = serialize(document)
    item.pop("local_path", None)
    return item


@router.get("/{rfi_id}/attachments")
async def list_rfi_attachments(rfi_id: UUID, _: dict = Depends(require_permission("RECEIPTS_READ"))):
    await _rfi_or_404(rfi_id)
    return [_public_rfi_response(item) async for item in get_database().rfi_attachments.find(
        {"rfi_id": rfi_id, "deleted_at": None}).sort("created_at", -1)]


@router.post("/{rfi_id}/attachments", status_code=201)
async def add_rfi_attachment(rfi_id: UUID, file: UploadFile = File(...),
                             actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    rfi = await _rfi_or_404(rfi_id)
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP, or PDF file")
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "File is larger than the configured upload limit")
    filename = _safe_filename(file.filename or "attachment")
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
    document = {"_id": uuid4(), "rfi_id": rfi_id, "filename": filename,
                "mime_type": file.content_type, "size_bytes": len(content),
                "storage_provider": provider, **storage, "created_by": actor["_id"],
                "created_at": utcnow(), "deleted_at": None}
    await get_database().rfi_attachments.insert_one(document)
    await write_audit("RFI_ATTACHMENT_UPLOADED", actor["_id"], "rfi_attachment", document["_id"],
                      metadata={"rfi_id": str(rfi_id)})
    return _public_rfi_response(document)


@router.get("/{rfi_id}/attachments/{attachment_id}/file")
async def download_rfi_attachment(rfi_id: UUID, attachment_id: UUID,
                                  _: dict = Depends(require_permission("RECEIPTS_READ"))):
    await _rfi_or_404(rfi_id)
    item = await get_database().rfi_attachments.find_one({"_id": attachment_id, "rfi_id": rfi_id,
                                                          "deleted_at": None})
    if item is None:
        raise HTTPException(404, "RFI attachment not found")
    if item["storage_provider"] == "google_drive":
        return {"web_url": item["web_url"]}
    path = Path(item["local_path"])
    if not path.is_file():
        raise HTTPException(404, "RFI attachment file is no longer available")
    return FileResponse(path, media_type=item["mime_type"], filename=item["filename"],
                        content_disposition_type="inline")


@router.delete("/{rfi_id}/attachments/{attachment_id}")
async def delete_rfi_attachment(rfi_id: UUID, attachment_id: UUID,
                                actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    await _rfi_or_404(rfi_id)
    result = await get_database().rfi_attachments.update_one(
        {"_id": attachment_id, "rfi_id": rfi_id, "deleted_at": None},
        {"$set": {"deleted_at": utcnow()}})
    if not result.matched_count:
        raise HTTPException(404, "RFI attachment not found")
    await write_audit("RFI_ATTACHMENT_DELETED", actor["_id"], "rfi_attachment", attachment_id,
                      metadata={"rfi_id": str(rfi_id)})
    return {"success": True}


@router.get("/{rfi_id}/responses")
async def list_rfi_responses(rfi_id: UUID, _: dict = Depends(require_permission("RECEIPTS_READ"))):
    await _rfi_or_404(rfi_id)
    return [_public_rfi_response(item) async for item in get_database().rfi_responses.find(
        {"rfi_id": rfi_id, "deleted_at": None}).sort("responded_at", 1)]


@router.post("/{rfi_id}/responses", status_code=201)
async def add_rfi_response(
    rfi_id: UUID, response_text: str = Form(""), responder_name: str = Form(...),
    responded_at: date = Form(default_factory=date.today), file: UploadFile | None = File(None),
    actor: dict = Depends(require_permission("RECEIPTS_UPDATE")),
):
    rfi = await _rfi_or_404(rfi_id)
    answer = response_text.strip()
    responder = responder_name.strip()
    if not responder or len(responder) > 200:
        raise HTTPException(422, "Responder name is required and must be at most 200 characters")
    if len(answer) > 10000:
        raise HTTPException(422, "Response text is too long")
    if not answer and file is None:
        raise HTTPException(422, "Add a response or an attachment")
    attachment: dict = {}
    if file is not None:
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
        attachment = {"filename": filename, "mime_type": file.content_type, "size_bytes": len(content),
                      "storage_provider": provider, **storage}
    response = {"_id": uuid4(), "rfi_id": rfi_id, "response_text": answer,
                "responder_name": responder, "responded_at": datetime.combine(responded_at, time.min, tzinfo=timezone.utc),
                "created_at": utcnow(), "created_by": actor["_id"], "deleted_at": None, **attachment}
    await get_database().rfi_responses.insert_one(response)
    await write_audit("RFI_RESPONSE_ADDED", actor["_id"], "rfi_response", response["_id"],
                      metadata={"rfi_id": str(rfi_id)})
    return _public_rfi_response(response)


@router.get("/{rfi_id}/responses/{response_id}/file")
async def download_rfi_response_file(rfi_id: UUID, response_id: UUID,
                                     _: dict = Depends(require_permission("RECEIPTS_READ"))):
    await _rfi_or_404(rfi_id)
    item = await get_database().rfi_responses.find_one({"_id": response_id, "rfi_id": rfi_id,
                                                        "deleted_at": None})
    if item is None or not item.get("filename"):
        raise HTTPException(404, "Response file not found")
    if item["storage_provider"] == "google_drive":
        return {"web_url": item["web_url"]}
    path = Path(item["local_path"])
    if not path.is_file():
        raise HTTPException(404, "Response file is no longer available")
    return FileResponse(path, media_type=item["mime_type"], filename=item["filename"],
                        content_disposition_type="inline")


@router.patch("/{receipt_id}")
async def update_receipt(receipt_id: UUID, payload: ReceiptUpdate, actor: dict = Depends(require_permission("RECEIPTS_UPDATE"))):
    changes = payload.model_dump(exclude_none=True)
    if "link_type" in changes or "link_id" in changes:
        current = await get_database().receipts.find_one({"_id": receipt_id, "deleted_at": None})
        if not current:
            raise HTTPException(404, "Receipt not found")
        changes["link_id"] = await _validate_job_link(changes.get("link_type", current.get("link_type")),
                                                      changes.get("link_id", current.get("link_id")))
    if "category" in changes and changes["category"] not in CATEGORIES:
        raise HTTPException(422, "Invalid category")
    if "transaction_type" in changes and changes["transaction_type"] not in {"expense", "income"}:
        raise HTTPException(422, "Invalid transaction type")
    if isinstance(changes.get("incurred_at"), date):
        changes["incurred_at"] = datetime.combine(changes["incurred_at"], time.min, tzinfo=timezone.utc)
    changes["updated_at"] = utcnow()
    item = await get_database().receipts.find_one_and_update({"_id": receipt_id, "deleted_at": None}, {"$set": changes}, return_document=ReturnDocument.AFTER)
    if not item:
        raise HTTPException(404, "Receipt not found")
    await write_audit("RECEIPT_UPDATED", actor["_id"], "receipt", receipt_id, changes)
    return serialize(item)


@router.delete("/{receipt_id}")
async def delete_receipt(receipt_id: UUID, actor: dict = Depends(require_permission("RECEIPTS_DELETE"))):
    result = await get_database().receipts.update_one({"_id": receipt_id, "deleted_at": None}, {"$set": {"deleted_at": utcnow()}})
    if not result.matched_count:
        raise HTTPException(404, "Receipt not found")
    await write_audit("RECEIPT_DELETED", actor["_id"], "receipt", receipt_id)
    return {"success": True}


@router.get("/reports/summary")
async def receipt_summary(year: int | Literal["all"] = date.today().year, job_id: UUID | None = None, currency: str | None = None,
                          _: dict = Depends(require_permission("RECEIPTS_READ"))):
    if year != "all" and (year < 1900 or year > 9998):
        raise HTTPException(422, "Year must be between 1900 and 9998")
    match = {"deleted_at": None, "document_type": "receipt", "amount": {"$ne": None}}
    if year != "all":
        start, end = datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        match["incurred_at"] = {"$gte": start, "$lt": end}
    job = None
    if job_id is not None:
        job = await get_database().jobs.find_one({"_id": job_id, "deleted_at": None})
        if job is None:
            raise HTTPException(404, "Job not found")
        match.update({"link_type": "job", "link_id": {"$in": [job["code"], str(job_id)]}})
    items = [item async for item in get_database().receipts.find(match)]
    currencies = sorted({(item.get("currency") or "CAD").upper() for item in items})
    if currency is not None:
        currency = currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise HTTPException(422, "Currency must be a three-letter code")
        items = [item for item in items if (item.get("currency") or "CAD").upper() == currency]
    elif len(currencies) > 1:
        return {"year": year, "job_id": str(job_id) if job else None,
                "job_code": job["code"] if job else None, "currency": None,
                "currencies": currencies, "mixed_currency": True,
                "income": None, "expenses": None, "profit_loss": None,
                "categories": {}, "months": [], "weeks": [], "years": []}
    report_currency = currency or (currencies[0] if currencies else "CAD")
    income = expenses = 0.0
    categories = {name: 0.0 for name in sorted(CATEGORIES)}
    month_totals = {value: {"income": 0.0, "expenses": 0.0} for value in range(1, 13)}
    week_totals: dict[int, dict[str, float]] = {}
    year_totals: dict[int, dict[str, float]] = {}
    for item in items:
        amount = float(item["amount"])
        bucket = "income" if item.get("transaction_type") == "income" else "expenses"
        if bucket == "income":
            income += amount
        else:
            expenses += amount
            category = item.get("category")
            if category in categories:
                categories[category] += amount
        incurred_at = item["incurred_at"]
        month_totals[incurred_at.month][bucket] += amount
        week = incurred_at.isocalendar().week
        week_totals.setdefault(week, {"income": 0.0, "expenses": 0.0})[bucket] += amount
        year_totals.setdefault(incurred_at.year, {"income": 0.0, "expenses": 0.0})[bucket] += amount
    months = [{"month": value, **month_totals[value]} for value in range(1, 13)]
    weeks = [{"week": value, **week_totals[value]} for value in sorted(week_totals)]
    years = [{"year": value, **year_totals[value]} for value in sorted(year_totals)]
    return {"year": year, "job_id": str(job_id) if job else None,
            "job_code": job["code"] if job else None,
            "currency": report_currency, "currencies": currencies, "mixed_currency": False,
            "income": round(income, 2), "expenses": round(expenses, 2),
            "profit_loss": round(income - expenses, 2), "categories": categories,
            "months": months if year != "all" else [], "weeks": weeks if year != "all" else [], "years": years}
