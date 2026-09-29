"""Render a quote or invoice snapshot matching Direct Connections design."""

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
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

    # Color Palette matching Direct Connections invoice
    primary_blue = colors.HexColor("#1d4ed8")      # Royal blue for INVOICE / QUOTE title & total
    dark_slate = colors.HexColor("#0f172a")        # Dark slate for company name & main text
    muted_text = colors.HexColor("#475569")        # Muted gray for address & details
    light_muted = colors.HexColor("#64748b")       # Section labels & descriptions
    accent_blue = colors.HexColor("#2563eb")       # Divider line blue
    header_tint = colors.HexColor("#e0f2fe")       # Ice blue header background
    header_text_color = colors.HexColor("#0284c7") # Header column text blue
    line_gray = colors.HexColor("#e2e8f0")         # Subtle table border gray

    # Typography styles
    body_style = ParagraphStyle("Body", fontName="QuoteRegular", fontSize=8.5, leading=11.5, textColor=dark_slate)
    body_bold = ParagraphStyle("BodyBold", parent=body_style, fontName="QuoteBold")
    desc_style = ParagraphStyle("Description", parent=body_style, fontSize=7.5, leading=10, textColor=light_muted)
    small_style = ParagraphStyle("Small", parent=body_style, fontSize=7.2, leading=9.5, textColor=muted_text)

    right_style = ParagraphStyle("Right", parent=body_style, alignment=TA_RIGHT)
    right_bold = ParagraphStyle("RightBold", parent=body_bold, alignment=TA_RIGHT)
    center_style = ParagraphStyle("Center", parent=body_style, alignment=TA_CENTER)
    
    th_left = ParagraphStyle("THLeft", fontName="QuoteBold", fontSize=8, leading=10, textColor=header_text_color)
    th_right = ParagraphStyle("THRight", parent=th_left, alignment=TA_RIGHT)
    th_center = ParagraphStyle("THCenter", parent=th_left, alignment=TA_CENTER)

    def safe(value: str | None) -> str:
        return escape(str(value or "")).replace("\r\n", "\n").replace("\n", "<br/>")

    def money(value: float | int | Decimal | str | None) -> str:
        if value is None:
            return "$0.00"
        return f"${Decimal(str(value)):,.2f}"

    # References and dates
    quote_id = str(quote.get("_id", ""))
    ref_num = quote_id[:8].upper() if len(quote_id) > 8 else (quote_id.upper() or "001")
    job_code = str(job.get("code", "")).strip() or "JOB"
    job_name = str(job.get("name", "")).strip() or "Client Project"
    company_name = str(job.get("company", "")).strip()
    
    updated = quote.get("updated_at") or quote.get("created_at")
    if isinstance(updated, datetime):
        date_str = updated.strftime("%B %d, %Y")
    else:
        date_str = datetime.now().strftime("%B %d, %Y")

    doc_title = str(quote.get("title", "")).strip()
    is_invoice = "invoice" in doc_title.lower()
    doc_type_heading = "INVOICE" if is_invoice else "QUOTE"
    doc_number_label = "Invoice #" if is_invoice else "Quote #"
    doc_number_val = f"INV{ref_num}" if is_invoice else f"QTE-{ref_num}"

    # Printable width = 612 - 80 = 532pt
    doc = SimpleDocTemplate(
        output,
        pagesize=letter,
        leftMargin=40,
        rightMargin=40,
        topMargin=32,
        bottomMargin=36,
        title=f"{doc_type_heading} {doc_number_val} - {job_name}",
        author="Direct Connections",
        pageCompression=1,
    )

    story = []

    # -------------------------------------------------------------
    # 1. HEADER BLOCK (Company on Left, Document Metadata on Right)
    # -------------------------------------------------------------
    company_html = (
        '<b><font size="14" color="#0f172a">Direct Connections</font></b><br/>'
        '<font size="8.5" color="#334155">Devante Williams-Morris</font><br/>'
        '<font size="7.5" color="#475569">GST/HST #: 707729422RT0001</font><br/>'
        '<font size="7.5" color="#475569">906-2301 Derry Road West</font><br/>'
        '<font size="7.5" color="#475569">Mississauga, ON, Canada L5N 2R4</font><br/>'
        '<font size="7.5" color="#475569">647-836-9906 · Devantetheelectrician@gmail.com</font>'
    )

    meta_html = (
        f'<b><font size="19" color="#1d4ed8">{doc_type_heading}</font></b><br/>'
        f'<font size="8" color="#0f172a"><b>{doc_number_label}:</b> {doc_number_val}</font><br/>'
        f'<font size="7.5" color="#475569"><b>Ref estimate:</b> EST{ref_num}</font><br/>'
        f'<font size="7.5" color="#475569"><b>Date:</b> {date_str}</font><br/>'
        f'<font size="7.5" color="#475569"><b>PO #:</b> {safe(job_code)}</font><br/>'
        f'<font size="7.5" color="#475569"><b>Currency:</b> CAD</font>'
    )

    header_table = Table(
        [
            [Paragraph(company_html, body_style), Paragraph(meta_html, right_style)]
        ],
        colWidths=[322, 210],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 8))

    # Divider bar
    divider = Table([[""]], colWidths=[532], rowHeights=[1.8])
    divider.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent_blue),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(divider)
    story.append(Spacer(1, 10))

    # -------------------------------------------------------------
    # 2. BILL TO & PAYMENT BLOCK
    # -------------------------------------------------------------
    bill_to_parts = [
        '<font size="7.5" color="#64748b"><b>BILL TO</b></font><br/>',
        f'<b><font size="9" color="#0f172a">{safe(job_name)}</font></b><br/>',
    ]
    if company_name:
        bill_to_parts.append(f'<font size="8" color="#334155">{safe(company_name)}</font><br/>')
    if job.get("description"):
        bill_to_parts.append(f'<font size="7.5" color="#64748b">{safe(job.get("description"))}</font>')

    payment_html = (
        '<font size="7.5" color="#64748b"><b>PAYMENT</b></font><br/>'
        '<font size="8.5" color="#0f172a">Due on receipt</font><br/>'
        f'<font size="7.5" color="#64748b">Please reference {doc_number_val} / PO {safe(job_code)}</font>'
    )

    info_table = Table(
        [
            [Paragraph("".join(bill_to_parts), body_style), Paragraph(payment_html, body_style)]
        ],
        colWidths=[322, 210],
    )
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 10))

    # -------------------------------------------------------------
    # 3. LINE ITEMS TABLE
    # -------------------------------------------------------------
    # Total width = 532 (247 + 75 + 45 + 65 + 100)
    col_widths = [247, 75, 45, 65, 100]
    
    rows = [
        [
            Paragraph("DESCRIPTION", th_left),
            Paragraph("RATE", th_right),
            Paragraph("QTY", th_center),
            Paragraph("DISCOUNT", th_right),
            Paragraph("AMOUNT", th_right),
        ]
    ]

    items_list = quote.get("items") or []
    subtotal_val = Decimal("0.00")

    for item in items_list:
        name = " ".join(str(item.get("name", "")).split())
        desc = str(item.get("description", "")).strip()
        qty = Decimal(str(item.get("quantity", 1))).normalize()
        rate = Decimal(str(item.get("unit_price", 0)))
        line_total = Decimal(str(item.get("line_total", rate * qty)))
        subtotal_val += line_total

        desc_content = [Paragraph(f"<b>{safe(name)}</b>", body_style)]
        if desc:
            desc_content.extend([Spacer(1, 1.5), Paragraph(safe(desc), desc_style)])
        
        source = item.get("source_url", "")
        if source:
            try:
                parsed = urlparse(source)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    desc_content.extend([
                        Spacer(1, 1.5),
                        Paragraph(f'<link href={quoteattr(source)} color="#2563eb">Product link ↗</link>', small_style)
                    ])
            except ValueError:
                pass

        qty_str = format(qty, "f")
        rows.append([
            desc_content,
            Paragraph(money(rate), right_style),
            Paragraph(qty_str, center_style),
            Paragraph("—", right_style),
            Paragraph(f"<b>{money(line_total)}</b>", right_bold),
        ])

    if not items_list:
        rows.append([
            Paragraph("<i>No items listed</i>", desc_style),
            Paragraph("—", right_style),
            Paragraph("—", center_style),
            Paragraph("—", right_style),
            Paragraph("$0.00", right_style),
        ])

    items_table = Table(rows, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_tint),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, header_text_color),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, line_gray),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 6))

    # -------------------------------------------------------------
    # 4. TOTALS & SUMMARY BLOCK (Subtotal, HST 13%, Total Due)
    # -------------------------------------------------------------
    if "total" in quote and quote["total"] is not None:
        calc_subtotal = Decimal(str(quote["total"]))
    else:
        calc_subtotal = subtotal_val

    hst_rate = Decimal("0.13")
    hst_amount = (calc_subtotal * hst_rate).quantize(Decimal("0.01"))
    total_due = calc_subtotal + hst_amount

    summary_rows = [
        [
            "",
            Paragraph("Subtotal", right_style),
            Paragraph(f"{money(calc_subtotal)} CAD", right_bold),
        ],
        [
            "",
            Paragraph("HST (13%)", right_style),
            Paragraph(f"{money(hst_amount)} CAD", right_bold),
        ],
    ]

    summary_table = Table(summary_rows, colWidths=[252, 140, 140])
    summary_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 4))

    # Grand Total Highlight Bar
    total_bar_data = [
        [
            Paragraph('<b><font size="10" color="#0f172a">TOTAL DUE (incl. tax)</font></b>', body_style),
            Paragraph(f'<b><font size="13" color="#1d4ed8">{money(total_due)} CAD</font></b>', right_style),
        ]
    ]
    total_bar = Table(total_bar_data, colWidths=[266, 266])
    total_bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), header_tint),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(KeepTogether([total_bar]))

    # -------------------------------------------------------------
    # 5. FOOTER TEXT / NOTES
    # -------------------------------------------------------------
    footer_text = (
        f"Converted from {doc_type_heading.title()} {doc_number_val} for Job {safe(job_code)}. "
        f"Subtotal {money(calc_subtotal)} + HST 13% {money(hst_amount)} = CAD {money(total_due)} total due. "
        "Thank you for your business."
    )
    story.append(Spacer(1, 10))
    story.append(Paragraph(footer_text, small_style))

    if quote.get("notes"):
        notes_html = f"<b>Notes:</b> {safe(quote.get('notes'))}"
        story.append(Spacer(1, 6))
        story.append(Paragraph(notes_html, small_style))

    # Page Header / Footer callback
    def on_page(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(line_gray)
        canvas.setLineWidth(0.5)
        canvas.line(40, 24, 572, 24)
        canvas.setFont("QuoteRegular", 7.5)
        canvas.setFillColor(muted_text)
        canvas.drawString(40, 14, f"Direct Connections · {doc_number_val} · All amounts in CAD")
        canvas.drawRightString(572, 14, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return output.getvalue()
