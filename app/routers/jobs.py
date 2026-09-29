"""Jobs collect business documents without changing receipt accounting."""

import asyncio
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.models import serialize, utcnow
from app.security import require_permission, user_permissions
from app.services.drive import DriveStorage
from app.services.currency import rates_for
from app.services.product_lookup import extract_product_info
from app.services.quote_pdf import render_quote_pdf

router = APIRouter(prefix="/jobs", tags=["jobs"])
settings = get_settings()
FILE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


class JobCreate(BaseModel):
    code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=200)
    company: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    budget_amount: float | None = Field(default=None, ge=0, le=1000000000, allow_inf_nan=False)
    budget_currency: str = Field(default="CAD", pattern=r"^[A-Za-z]{3}$")


class JobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern=r"^(active|completed|archived)$")
    budget_amount: float | None = Field(default=None, ge=0, le=1000000000, allow_inf_nan=False)
    budget_currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")


class RfiCreate(BaseModel):
    rfi_number: str = Field(default="", max_length=80)
    rfi_subject: str = Field(min_length=1, max_length=200)
    rfi_question: str = Field(min_length=1, max_length=5000)
    rfi_to: str = Field(default="", max_length=1000)
    rfi_attention_name: str = Field(default="", max_length=200)
    rfi_attention_phone: str = Field(default="", max_length=80)
    rfi_attention_email: str = Field(default="", max_length=200)
    incurred_at: date = Field(default_factory=date.today)
    rfi_due_at: date | None = None


class QuoteItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    source_url: str = Field(default="", max_length=2000)
    quantity: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    unit_price: float = Field(ge=0, le=100000000, allow_inf_nan=False)


class QuotePayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=5000)
    items: list[QuoteItem] = Field(min_length=1, max_length=500)


class ProductLookupRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


def _quote_fields(payload: QuotePayload) -> dict:
    if not payload.title.strip():
        raise HTTPException(422, "Quote title is required")
    items = []
    total = Decimal("0")
    for item in payload.items:
        name = item.name.strip()
        if not name:
            raise HTTPException(422, "Each quote item needs a name")
        quantity = Decimal(str(item.quantity))
        price = Decimal(str(item.unit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        line_total = (quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += line_total
        items.append({"name": name, "description": item.description.strip(),
                      "source_url": item.source_url.strip(),
                      "quantity": item.quantity, "unit_price": float(price),
                      "line_total": float(line_total)})
    return {"title": payload.title.strip(), "notes": payload.notes.strip(),
            "items": items, "total": float(total.quantize(Decimal("0.01"))), "currency": "CAD"}


async def _job_or_404(job_id: UUID) -> dict:
    job = await get_database().jobs.find_one({"_id": job_id, "deleted_at": None})
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _public_file(document: dict) -> dict:
    item = serialize(document)
    item.pop("local_path", None)
    return item


def _public_quote(document: dict) -> dict:
    item = serialize({key: value for key, value in document.items() if key != "pdf_content"})
    item["pdf_url"] = f"/jobs/{document['job_id']}/quotes/{document['_id']}/pdf"
    return item


async def _quote_pdf_fields(quote: dict, job: dict) -> dict:
    content = await asyncio.to_thread(render_quote_pdf, quote, job)
    # Keep the PDF plus quote data comfortably below MongoDB's document limit.
    if len(content) > 8_000_000:
        raise HTTPException(413, "Quote PDF is too large; split this quote into smaller quotes")
    title = re.sub(r"[^A-Za-z0-9._-]", "_", quote["title"])[:80].strip("._") or "quote"
    return {"pdf_content": content, "pdf_filename": f"{title}-{str(quote['_id'])[:8]}.pdf",
            "pdf_generated_at": quote["updated_at"]}


def _budget_summary(job: dict, documents: list[dict], can_read_receipts: bool,
                    exchange_rates: dict[str, float] | None = None) -> dict:
    expense_receipts = [item for item in documents if item.get("document_type") == "receipt"
                        and item.get("transaction_type") == "expense" and item.get("amount") is not None]
    currencies = sorted({(item.get("currency") or "CAD").upper() for item in expense_receipts})
    currency = (job.get("budget_currency") or (currencies[0] if len(currencies) == 1 else "CAD")).upper()
    amount = job.get("budget_amount")
    rates = exchange_rates or {currency: 1.0}
    unavailable = [code for code in currencies if code not in rates]
    converted = [code for code in currencies if code != currency and code in rates]
    summary = {"amount": amount, "currency": currency, "expense_currencies": currencies,
               "other_currencies": unavailable, "converted_currencies": converted,
               "exchange_rates": {code: rates[code] for code in converted}}
    if not can_read_receipts:
        return {**summary, "spent": None, "remaining": None, "over_budget": None,
                "receipt_count": None, "requires_receipt_access": True}
    matching_receipts = [item for item in expense_receipts
                         if (item.get("currency") or "CAD").upper() in rates]
    spent = sum((Decimal(str(item["amount"])) * Decimal(str(rates[(item.get("currency") or "CAD").upper()]))
                 for item in matching_receipts), Decimal("0"))
    spent = spent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    remaining = (Decimal(str(amount)) - spent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if amount is not None else None
    return {**summary, "spent": float(spent), "remaining": float(remaining) if remaining is not None else None,
            "over_budget": remaining < 0 if remaining is not None else False,
            "receipt_count": len(matching_receipts),
            "requires_receipt_access": False}


@router.get("")
async def list_jobs(_: dict = Depends(require_permission("JOBS_READ"))):
    return [serialize(job) async for job in get_database().jobs.find({"deleted_at": None}).sort("updated_at", -1)]


@router.post("", status_code=201)
async def create_job(payload: JobCreate, actor: dict = Depends(require_permission("JOBS_CREATE"))):
    now = utcnow()
    job = {"_id": uuid4(), "code": payload.code.upper(), "name": payload.name.strip(),
           "company": payload.company.strip(),
           "description": payload.description.strip(), "budget_amount": payload.budget_amount,
           "budget_currency": payload.budget_currency.upper(), "status": "active", "created_by": actor["_id"],
           "created_at": now, "updated_at": now, "deleted_at": None}
    try:
        await get_database().jobs.insert_one(job)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "A job with this code already exists") from exc
    await write_audit("JOB_CREATED", actor["_id"], "job", job["_id"])
    return serialize(job)


@router.post("/{job_id}/rfis", status_code=201)
async def create_job_rfi(job_id: UUID, payload: RfiCreate,
                         actor: dict = Depends(require_permission("RECEIPTS_CREATE"))):
    job = await _job_or_404(job_id)
    if not payload.rfi_subject.strip() or not payload.rfi_question.strip():
        raise HTTPException(422, "RFI subject and question are required")
    now = utcnow()
    document = {"_id": uuid4(), "document_type": "rfi", "transaction_type": "expense",
                "category": "job_expense", "vendor": None, "amount": None, "currency": "CAD",
                "incurred_at": datetime.combine(payload.incurred_at, time.min, tzinfo=timezone.utc),
                "filename": None, "mime_type": None, "size_bytes": 0, "ocr_text": "",
                "link_type": "job", "link_id": job["code"], "link_label": job["name"],
                "rfi_number": payload.rfi_number.strip() or None,
                "rfi_subject": payload.rfi_subject.strip(), "rfi_question": payload.rfi_question.strip(),
                "rfi_to": payload.rfi_to.strip() or None,
                "rfi_attention_name": payload.rfi_attention_name.strip() or None,
                "rfi_attention_phone": payload.rfi_attention_phone.strip() or None,
                "rfi_attention_email": payload.rfi_attention_email.strip() or None,
                "rfi_due_at": datetime.combine(payload.rfi_due_at, time.min, tzinfo=timezone.utc)
                if payload.rfi_due_at else None,
                "status": "open", "closed_at": None,
                "storage_provider": None, "created_by": actor["_id"], "created_at": now,
                "updated_at": now, "deleted_at": None}
    await get_database().receipts.insert_one(document)
    await write_audit("RFI_CREATED", actor["_id"], "rfi", document["_id"])
    return serialize(document)


@router.get("/{job_id}")
async def get_job(job_id: UUID, actor: dict = Depends(require_permission("JOBS_READ"))):
    job = await _job_or_404(job_id)
    permissions = await user_permissions(actor)
    can_read_receipts = "*" in permissions or "RECEIPTS_READ" in permissions
    db = get_database()
    document_cursor = db.receipts.find({
            "deleted_at": None, "link_type": "job", "link_id": {"$in": [job["code"], str(job_id)]},
        }).sort("incurred_at", -1) if can_read_receipts else None
    file_cursor = db.job_files.find({
        "job_id": job_id, "deleted_at": None,
    }).sort("created_at", -1)
    quote_cursor = db.quotes.find({
        "job_id": job_id, "deleted_at": None,
    }, {"pdf_content": 0}).sort("created_at", -1)
    raw_documents = []
    if document_cursor is not None:
        raw_documents, raw_files, raw_quotes = await asyncio.gather(
            document_cursor.to_list(length=None), file_cursor.to_list(length=None),
            quote_cursor.to_list(length=None))
    else:
        raw_files, raw_quotes = await asyncio.gather(
            file_cursor.to_list(length=None), quote_cursor.to_list(length=None))
    rfi_ids_with_responses = set(await db.rfi_responses.distinct("rfi_id", {"deleted_at": None}))
    documents = []
    for item in raw_documents:
        if item.get("document_type") == "rfi" and (item.get("_id") in rfi_ids_with_responses or item.get("status") == "closed"):
            item["status"] = "closed"
        documents.append(serialize(item))
    files = [_public_file(item) for item in raw_files]
    quotes = [_public_quote(item) for item in raw_quotes]
    budget_currency = (job.get("budget_currency") or "CAD").upper()
    expense_currencies = {(doc.get("currency") or "CAD").upper() for doc in documents
                          if doc.get("document_type") == "receipt"
                          and doc.get("transaction_type") == "expense" and doc.get("amount") is not None}
    exchange_rates = await rates_for(expense_currencies, budget_currency) if can_read_receipts else None
    return {"job": serialize(job),
            "budget": _budget_summary(job, documents, can_read_receipts, exchange_rates),
            "receipts": [doc for doc in documents if doc["document_type"] == "receipt"],
            "rfis": [doc for doc in documents if doc["document_type"] == "rfi"],
            "quotes": quotes + [file for file in files if file["kind"] == "quote"],
            "drawings": [file for file in files if file["kind"] == "drawing"]}


@router.post("/{job_id}/quotes", status_code=201)
async def create_quote(job_id: UUID, payload: QuotePayload,
                       actor: dict = Depends(require_permission("JOBS_UPDATE"))):
    job = await _job_or_404(job_id)
    now = utcnow()
    quote = {"_id": uuid4(), "job_id": job_id, "kind": "quote", "record_type": "structured_quote",
             **_quote_fields(payload), "created_by": actor["_id"], "created_at": now,
             "updated_at": now, "deleted_at": None}
    quote.update(await _quote_pdf_fields(quote, job))
    await get_database().quotes.insert_one(quote)
    await write_audit("QUOTE_CREATED", actor["_id"], "quote", quote["_id"],
                      metadata={"job_id": str(job_id)})
    return _public_quote(quote)


@router.post("/{job_id}/quotes/extract-item")
async def extract_quote_item(job_id: UUID, payload: ProductLookupRequest,
                             _: dict = Depends(require_permission("JOBS_UPDATE"))):
    await _job_or_404(job_id)
    return await extract_product_info(payload.url)


@router.patch("/{job_id}/quotes/{quote_id}")
async def update_quote(job_id: UUID, quote_id: UUID, payload: QuotePayload,
                       actor: dict = Depends(require_permission("JOBS_UPDATE"))):
    job = await _job_or_404(job_id)
    existing = await get_database().quotes.find_one(
        {"_id": quote_id, "job_id": job_id, "deleted_at": None}, {"pdf_content": 0})
    if existing is None:
        raise HTTPException(404, "Quote not found")
    changes = {**_quote_fields(payload), "updated_at": utcnow()}
    changes.update(await _quote_pdf_fields({**existing, **changes}, job))
    quote = await get_database().quotes.find_one_and_update(
        {"_id": quote_id, "job_id": job_id, "deleted_at": None}, {"$set": changes},
        return_document=ReturnDocument.AFTER)
    if quote is None:
        raise HTTPException(404, "Quote not found")
    await write_audit("QUOTE_UPDATED", actor["_id"], "quote", quote_id,
                      metadata={"job_id": str(job_id)})
    return _public_quote(quote)


@router.get("/{job_id}/quotes/{quote_id}/pdf")
async def download_quote_pdf(job_id: UUID, quote_id: UUID,
                             actor: dict = Depends(require_permission("JOBS_READ"))):
    job = await _job_or_404(job_id)
    quote = await get_database().quotes.find_one(
        {"_id": quote_id, "job_id": job_id, "deleted_at": None})
    if quote is None:
        raise HTTPException(404, "Quote not found")
    # Older quotes remain downloadable without a data migration.
    pdf = quote if quote.get("pdf_content") else await _quote_pdf_fields(quote, job)
    await write_audit("QUOTE_PDF_DOWNLOADED", actor["_id"], "quote", quote_id,
                      metadata={"job_id": str(job_id)})
    return Response(content=pdf["pdf_content"], media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{pdf["pdf_filename"]}"',
                             "Cache-Control": "private, no-store"})


@router.delete("/{job_id}/quotes/{quote_id}")
async def delete_quote(job_id: UUID, quote_id: UUID,
                       actor: dict = Depends(require_permission("JOBS_UPDATE"))):
    result = await get_database().quotes.update_one(
        {"_id": quote_id, "job_id": job_id, "deleted_at": None},
        {"$set": {"deleted_at": utcnow()}})
    if not result.matched_count:
        raise HTTPException(404, "Quote not found")
    await write_audit("QUOTE_DELETED", actor["_id"], "quote", quote_id,
                      metadata={"job_id": str(job_id)})
    return {"success": True}


@router.patch("/{job_id}")
async def update_job(job_id: UUID, payload: JobUpdate, actor: dict = Depends(require_permission("JOBS_UPDATE"))):
    changes = payload.model_dump(exclude_none=True)
    if "budget_amount" in payload.model_fields_set and payload.budget_amount is None:
        changes["budget_amount"] = None
    if not changes:
        raise HTTPException(422, "No changes supplied")
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    if "company" in changes:
        changes["company"] = changes["company"].strip()
    if "description" in changes:
        changes["description"] = changes["description"].strip()
    if "budget_currency" in changes:
        changes["budget_currency"] = changes["budget_currency"].upper()
    changes["updated_at"] = utcnow()
    job = await get_database().jobs.find_one_and_update({"_id": job_id, "deleted_at": None},
        {"$set": changes}, return_document=ReturnDocument.AFTER)
    if job is None:
        raise HTTPException(404, "Job not found")
    await write_audit("JOB_UPDATED", actor["_id"], "job", job_id, changes)
    return serialize(job)


@router.post("/{job_id}/files", status_code=201)
async def upload_job_file(job_id: UUID, kind: str = Form(...), file: UploadFile = File(...),
                          actor: dict = Depends(require_permission("JOBS_UPDATE"))):
    job = await _job_or_404(job_id)
    if kind not in {"quote", "drawing"}:
        raise HTTPException(422, "File kind must be quote or drawing")
    if file.content_type not in FILE_TYPES:
        raise HTTPException(415, "Upload a PDF, JPEG, PNG, or WebP file")
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "File is larger than the configured upload limit")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "upload").name)[:180] or "upload"
    file_id = uuid4()
    if settings.google_drive_credentials_json and settings.google_drive_root_folder_id:
        drive = DriveStorage(settings.google_drive_credentials_json, settings.google_drive_root_folder_id)
        storage = await asyncio.to_thread(drive.upload, content, filename, file.content_type,
                                           ["Jobs", job["code"], "Quotes" if kind == "quote" else "Drawings"])
        provider = "google_drive"
    else:
        target = Path(settings.local_upload_directory) / "Jobs" / str(job_id) / kind
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{file_id}-{filename}"
        await asyncio.to_thread(path.write_bytes, content)
        storage = {"local_path": str(path)}
        provider = "local"
    now = utcnow()
    document = {"_id": file_id, "job_id": job_id, "kind": kind, "filename": filename,
                "mime_type": file.content_type, "size_bytes": len(content), "storage_provider": provider,
                **storage, "created_by": actor["_id"], "created_at": now, "deleted_at": None}
    await get_database().job_files.insert_one(document)
    await write_audit("JOB_FILE_UPLOADED", actor["_id"], "job_file", file_id,
                      metadata={"job_id": str(job_id), "kind": kind})
    return _public_file(document)


@router.get("/{job_id}/files/{file_id}")
async def download_job_file(job_id: UUID, file_id: UUID, _: dict = Depends(require_permission("JOBS_READ"))):
    await _job_or_404(job_id)
    document = await get_database().job_files.find_one({"_id": file_id, "job_id": job_id, "deleted_at": None})
    if document is None:
        raise HTTPException(404, "File not found")
    if document["storage_provider"] == "google_drive":
        return {"web_url": document["web_url"]}
    path = Path(document["local_path"])
    if not path.is_file():
        raise HTTPException(404, "File is no longer available")
    return FileResponse(path, media_type=document["mime_type"], filename=document["filename"])


@router.delete("/{job_id}/files/{file_id}")
async def delete_job_file(job_id: UUID, file_id: UUID, actor: dict = Depends(require_permission("JOBS_UPDATE"))):
    await _job_or_404(job_id)
    result = await get_database().job_files.update_one({"_id": file_id, "job_id": job_id, "deleted_at": None},
                                                        {"$set": {"deleted_at": utcnow()}})
    if not result.matched_count:
        raise HTTPException(404, "File not found")
    await write_audit("JOB_FILE_DELETED", actor["_id"], "job_file", file_id)
    return {"success": True}
