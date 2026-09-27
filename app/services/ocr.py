import logging
import os
import re
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from tempfile import gettempdir
from threading import Lock
from typing import Any

from PIL import Image, ImageOps

from app.config import get_settings

_rapid_engine: Any = None
_paddle_engine: Any = None
_paddle_failed = False
_engine_lock = Lock()
logger = logging.getLogger(__name__)


def _extract_with_rapid(image: Image.Image) -> tuple[str, float | None]:
    global _rapid_engine
    if _rapid_engine is None:
        # Native OCR libraries are only required by OCR requests, not API startup.
        from rapidocr import RapidOCR

        params = None
        if os.environ.get("VERCEL") == "1":
            params = {"Global.model_root_dir": str(Path(gettempdir()) / "rapidocr-models")}
        _rapid_engine = RapidOCR(params=params)
    result = _rapid_engine(image)
    if not result or not result.txts:
        return "", None
    lines = [str(line).strip() for line in result.txts if line]
    scores = [float(score) for score in (result.scores if result.scores is not None else []) if score is not None]
    return "\n".join(lines), (sum(scores) / len(scores) if scores else None)


def _paddle_payload(result: Any) -> dict[str, Any]:
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
        payload = payload["res"]
    return payload if isinstance(payload, dict) else {}


def _get_paddle_engine() -> Any:
    global _paddle_engine
    if _paddle_engine is None:
        from paddleocr import PaddleOCR
        _paddle_engine = PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _paddle_engine


def _extract_with_paddle(image: Image.Image) -> tuple[str, float | None]:
    import numpy as np

    global _paddle_failed
    if _paddle_failed:
        raise RuntimeError("PaddleOCR is unavailable")
    try:
        output = _get_paddle_engine().predict(np.asarray(image))
        lines: list[str] = []
        scores: list[float] = []
        for item in output:
            payload = _paddle_payload(item)
            blocks = payload.get("parsing_res_list") or []
            lines.extend(str(block.get("block_content", "")).strip()
                         for block in blocks if isinstance(block, dict) and block.get("block_content"))
            overall = payload.get("overall_ocr_res") or payload
            if not lines:
                lines.extend(str(value).strip() for value in overall.get("rec_texts", []) if value)
            scores.extend(float(value) for value in overall.get("rec_scores", []) if value is not None)
        return "\n".join(line for line in lines if line), (sum(scores) / len(scores) if scores else None)
    except Exception:
        _paddle_failed = True
        raise


def warm_ocr_engine() -> None:
    """Load OCR models before the first receipt without retaining document data."""
    if get_settings().ocr_engine != "paddle":
        return
    with _engine_lock:
        _get_paddle_engine()


def extract_document(content: bytes) -> dict[str, str | float | None]:
    image = ImageOps.exif_transpose(Image.open(BytesIO(content))).convert("RGB")
    settings = get_settings()
    with _engine_lock:
        if settings.ocr_engine == "paddle":
            try:
                text, confidence = _extract_with_paddle(image)
                if text:
                    return {"text": text, "engine": "paddle-ppocrv5-mobile", "confidence": confidence}
            except Exception as exc:
                # Never log OCR text or image contents; only report why the local
                # high-accuracy engine could not start before using the fallback.
                logger.warning("PaddleOCR unavailable; using RapidOCR fallback: %s", exc)
        text, confidence = _extract_with_rapid(image)
        return {"text": text, "engine": "rapidocr-fallback", "confidence": confidence}


def extract_text(content: bytes) -> str:
    return str(extract_document(content)["text"])


_AMOUNT = re.compile(r"(?<!\d)([0-9]{1,3}(?:[.'\s][0-9]{3})*[,.][0-9]{2}|[0-9]{1,6}[,.][0-9]{2})(?!\d)")


def _parse_amount(value: str) -> float | None:
    clean = value.replace("'", "").replace(" ", "")
    if "," in clean and "." in clean:
        decimal = "," if clean.rfind(",") > clean.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        clean = clean.replace(thousands, "").replace(decimal, ".")
    elif "," in clean:
        clean = clean.replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return None


def suggested_total(text: str) -> float | None:
    """Find the payable total from labels used by many receipt formats."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    label = re.compile(
        r"\b(amount\s+due|balance\s+due|grand\s+total|total\s+due|total|montant\s+d[uû]|solde)\b",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = label.search(line)
        if not match or re.search(r"\b(subtotal|sous[- ]total)\b", line, re.IGNORECASE):
            continue
        # Start at the label so prices earlier on a flattened line cannot win.
        nearby = " ".join([line[match.start():], *lines[index + 1:index + 3]])
        for value in _AMOUNT.findall(nearby):
            parsed = _parse_amount(value)
            if parsed is not None:
                return parsed
    return None


def suggested_date(text: str) -> date | None:
    patterns = (
        (r"(?<!\d)(\d{4}[./-]\d{1,2}[./-]\d{1,2})(?!\d)", ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")),
        (r"\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y")),
        (r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b", ("%d %B %Y", "%d %b %Y")),
    )
    for pattern, formats in patterns:
        for value in re.findall(pattern, text):
            for date_format in formats:
                try:
                    return datetime.strptime(value, date_format).date()
                except ValueError:
                    pass

    # Values over 12 disambiguate regional ordering. For an ambiguous date,
    # month/day is the default because the primary audience is Canadian.
    for match in re.finditer(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)", text):
        first, second, year = (int(part) for part in match.groups())
        year = year + 2000 if year < 100 else year
        day, month = (first, second) if first > 12 else (second, first)
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def suggested_currency(text: str) -> str | None:
    upper = text.upper()
    matches: list[tuple[int, str]] = []
    for currency, pattern in (
        ("CAD", r"\bCAD\b|C\$"), ("USD", r"\bUSD\b|US\$"),
        ("EUR", r"\bEUR\b|€"), ("GBP", r"\bGBP\b|£"), ("CHF", r"\bCHF\b"),
        ("AUD", r"\bAUD\b|A\$"), ("JPY", r"\bJPY\b|¥"),
    ):
        match = re.search(pattern, upper)
        if match:
            matches.append((match.start(), currency))
    if matches:
        # Receipts sometimes print a secondary conversion near the bottom. The
        # first explicit currency normally belongs to the transaction itself.
        return min(matches)[1]
    # Bare dollar signs are ambiguous. Canadian sales-tax labels provide strong
    # evidence; otherwise CAD is the configured product default for this audience.
    if re.search(r"\b(GST|HST|QST|TPS|TVQ)\b", upper) or "$" in text:
        return "CAD"
    return None


def suggested_category(text: str) -> str:
    lower = text.lower()
    category_keywords = {
        "gas": ("gasoline", "petrol", "diesel", "fuel", "pump", "litre", "liter", "unleaded", "essence"),
        "client_meals": ("restaurant", "cafe", "coffee", "table", "tisch", "latte", "meal", "food", "bar", "server", "served", "gratuity", "tip"),
        "maintenance": ("repair", "maintenance", "service", "parts", "hardware", "automotive", "garage", "plumbing", "electrical"),
        "job_expense": ("job", "project", "work order", "site expense", "materials", "lumber", "supplies", "equipment", "tool"),
    }
    scores = {category: sum(1 for keyword in keywords if keyword in lower)
              for category, keywords in category_keywords.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "other"


def suggested_fields(text: str) -> dict[str, object | None]:
    detected_date = suggested_date(text)
    return {
        "suggested_vendor": suggested_vendor(text),
        "suggested_amount": suggested_total(text),
        "suggested_date": detected_date.isoformat() if detected_date else None,
        "suggested_category": suggested_category(text),
        "suggested_currency": suggested_currency(text),
        "suggested_transaction_type": "expense",
    }


def suggested_vendor(text: str) -> str | None:
    ignored = re.compile(
        r"\b(receipt|invoice|tax invoice|customer copy|merchant copy|order|transaction|"
        r"date|time|cashier|register|terminal|welcome|thank you|merci|total|subtotal|"
        r"gst|hst|qst|tps|tvq|phone|tel|fax|www\.|https?://)\b",
        re.IGNORECASE,
    )
    postal_or_address = re.compile(
        r"\b\d{1,6}\s+\w+|\b[A-Z]\d[A-Z][ -]?\d[A-Z]\d\b|\b\d{5}(?:-\d{4})?\b",
        re.IGNORECASE,
    )
    for line in text.splitlines()[:10]:
        clean = line.strip()
        if len(clean) < 2 or re.fullmatch(r"[\d\W]+", clean):
            continue
        candidate = re.split(r"\s{2,}", clean, maxsplit=1)[0].strip(" -*|:")
        if ignored.search(candidate) or postal_or_address.search(candidate):
            continue
        if sum(character.isdigit() for character in candidate) > len(candidate) / 3:
            continue
        return candidate[:160]
    return None
