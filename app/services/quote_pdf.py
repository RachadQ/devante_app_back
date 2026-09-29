"""Render a quote snapshot without network access or temporary files."""

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("QuoteRegular", str(_FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("QuoteBold", str(_FONT_DIR / "VeraBd.ttf")))
pdfmetrics.registerFontFamily("QuoteRegular", normal="QuoteRegular", bold="QuoteBold")


def render_quote_pdf(quote: dict, job: dict) -> bytes:
    output = BytesIO()
    navy = colors.HexColor("#172B4D")
    muted = colors.HexColor("#526174")
    body = ParagraphStyle("body", fontName="QuoteRegular", fontSize=9, leading=13, textColor=navy)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=11, textColor=muted)
    title = ParagraphStyle("title", parent=body, fontName="QuoteBold", fontSize=21, leading=27, spaceAfter=14)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT, fontSize=8, leading=12)
    label = ParagraphStyle("label", parent=small, fontName="QuoteBold", textColor=colors.white)

    def safe(value):
        return escape(str(value or "")).replace("\r\n", "\n").replace("\n", "<br/>")

    def paragraph(value, style=body):
        return Paragraph(safe(value), style)

    def money(value):
        return f"CA${Decimal(str(value)):,.2f}"

    reference = str(quote["_id"])[:8].upper()
    updated = quote.get("updated_at") or quote.get("created_at")
    date_text = updated.strftime("%b %d, %Y") if isinstance(updated, datetime) else ""
    document = SimpleDocTemplate(output, pagesize=letter, rightMargin=48, leftMargin=48,
                                 topMargin=44, bottomMargin=48, title=str(quote["title"]),
                                 author="Devante", pageCompression=1)
    story = [paragraph("DEVANTE  /  QUOTE", small), Spacer(1, 12), paragraph(" ".join(quote["title"].split()), title)]
    metadata = [[paragraph("JOB", small), paragraph("QUOTE REFERENCE", small)],
                [paragraph(f"{job['code']} - {job['name']}"), paragraph(reference)],
                [paragraph(job.get("company", "")), paragraph(date_text)]]
    table = Table(metadata, colWidths=[360, 156])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.extend([table, Spacer(1, 20)])
    rows = [[paragraph(text, label) for text in ("ITEM / DESCRIPTION", "QTY", "UNIT PRICE", "AMOUNT")]]
    for item in quote["items"]:
        fragments = []
        if item.get("description"):
            pending = paragraph(item["description"], small)
            while pending.wrap(238, 400)[1] > 400:
                head, pending = pending.split(238, 400)
                fragments.append(head)
            fragments.append(pending)
        else:
            fragments.append(None)
        source = item.get("source_url", "")
        try:
            parsed = urlparse(source)
            valid_link = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
        except ValueError:
            valid_link = False
        quantity = format(Decimal(str(item["quantity"])).normalize(), "f")
        for index, fragment in enumerate(fragments):
            name = " ".join(item["name"].split()) + (" (continued)" if index else "")
            details = [Paragraph(f"<b>{safe(name)}</b>", body)]
            if fragment is not None:
                details.extend([Spacer(1, 4), fragment])
            if valid_link and index == len(fragments) - 1:
                details.extend([Spacer(1, 4), Paragraph(f'<link href={quoteattr(source)} color="#136F63">Product link</link>', small)])
            rows.append([details, paragraph(quantity if index == 0 else "", right),
                         paragraph(money(item["unit_price"]) if index == 0 else "", right),
                         paragraph(money(item["line_total"]) if index == 0 else "", right)])
    items = Table(rows, colWidths=[258, 44, 100, 114], repeatRows=1, splitByRow=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, 0), 1, navy),
    ]))
    story.append(items)
    total = Table([[paragraph("TOTAL (CAD)"), paragraph(money(quote["total"]),
                   ParagraphStyle("total", parent=right, fontName="QuoteBold", fontSize=15, leading=20))]],
                  colWidths=[310, 206])
    total.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF4F1")),
                              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("TOPPADDING", (0, 0), (-1, -1), 12),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                              ("LEFTPADDING", (0, 0), (-1, -1), 12),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 12)]))
    story.append(KeepTogether([Spacer(1, 14), total]))
    if quote.get("notes"):
        notes_heading = ParagraphStyle("notes_heading", parent=body, keepWithNext=True, spaceAfter=6)
        story.extend([Spacer(1, 20), Paragraph("<b>Notes</b>", notes_heading), paragraph(quote["notes"])])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#DEE5ED"))
        canvas.line(48, 34, 564, 34)
        canvas.setFont("QuoteRegular", 8)
        canvas.setFillColor(muted)
        canvas.drawString(48, 22, f"Devante | Quote {reference} | All amounts in CAD")
        canvas.drawRightString(564, 22, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
