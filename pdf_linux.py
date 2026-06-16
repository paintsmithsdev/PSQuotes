"""Cross-platform PDF generation using ReportLab (no Microsoft Word required)."""

from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title",
            parent=base["Heading1"],
            fontSize=14,
            spaceAfter=6,
        ),
        "heading": ParagraphStyle(
            "Heading",
            parent=base["Heading2"],
            fontSize=11,
            spaceBefore=8,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["Normal"],
            fontSize=9,
            leading=11,
            spaceAfter=3,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["Normal"],
            fontSize=7,
            leading=8,
            spaceAfter=2,
        ),
        "cell": ParagraphStyle(
            "Cell",
            parent=base["Normal"],
            fontSize=8,
            leading=9,
            wordWrap="CJK",
        ),
        "cell_header": ParagraphStyle(
            "CellHeader",
            parent=base["Normal"],
            fontSize=8,
            leading=9,
            fontName="Helvetica-Bold",
        ),
    }


def _p(text, style="body"):
    return Paragraph(str(text or "").replace("\n", "<br/>"), _styles()[style])


def _table(data, col_widths=None, header_rows=1, font_size=8):
    styles = _styles()
    wrapped = []
    for r_idx, row in enumerate(data):
        wrapped_row = []
        for cell in row:
            if isinstance(cell, Paragraph):
                wrapped_row.append(cell)
            else:
                st = styles["cell_header"] if r_idx < header_rows else styles["cell"]
                wrapped_row.append(Paragraph(str(cell or ""), st))
        wrapped.append(wrapped_row)

    table = Table(wrapped, colWidths=col_widths, repeatRows=header_rows)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _build_pdf(elements, pagesize=A4):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    doc.build(elements)
    buffer.seek(0)
    return buffer


def _render_job_spec_body(job_no, total_man_days, sections, client_info):
    elements = []
    ci = client_info or {}
    left_lines = [
        "<b>Customer Info</b>",
        f"<b>Client:</b> {ci.get('client', '')}",
        f"<b>Phone:</b> {ci.get('phone', '')}",
        f"<b>Email:</b> {ci.get('email', '')}",
        f"<b>Address:</b> {ci.get('address', '')}",
    ]
    right_lines = [
        "<b>Area Manager Info</b>",
        f"<b>Area Manager:</b> {ci.get('area_manager', '')}",
        f"<b>Phone:</b> {ci.get('am_phone', '')}",
        f"<b>Email:</b> {ci.get('am_email', '')}",
    ]
    info_data = [
        [_p("<br/>".join(left_lines), "body"), _p("<br/>".join(right_lines), "body")],
    ]
    elements.append(_table(info_data, col_widths=[90 * mm, 90 * mm], header_rows=0))
    elements.append(Spacer(1, 4 * mm))
    elements.append(
        _table(
            [
                [
                    _p(f"<b>Quote Number:</b> {job_no}", "body"),
                    _p(
                        f"<b>Total Allowed Man-days:</b> {float(total_man_days or 0):.2f}",
                        "body",
                    ),
                ]
            ],
            col_widths=[90 * mm, 90 * mm],
            header_rows=0,
        )
    )
    elements.append(Spacer(1, 6 * mm))

    for sec in sections or []:
        elements.append(_p(sec.get("title", ""), "heading"))
        elements.append(_p(f"<b>Qty:</b> {sec.get('qty_line', '')}", "body"))
        elements.append(_p("<b>Job Notes(steps)</b>", "body"))
        steps = sec.get("job_note_steps") or []
        if not steps:
            for n in range(1, 4):
                elements.append(_p(f"{n}. .", "body"))
        else:
            for n, step in enumerate(steps, 1):
                elements.append(_p(f"{n}. {step}", "body"))
        elements.append(_p("<b>Notes:</b>", "body"))
        notes = str(sec.get("notes", "") or "").strip()
        if notes:
            elements.append(_p(notes, "body"))
        for _ in range(3):
            elements.append(_p("_" * 72, "body"))
        elements.append(
            _p(
                "<b>Completed & Checked:</b> "
                "Name__________________________  Signed___________________________",
                "body",
            )
        )
        elements.append(Spacer(1, 6 * mm))
    return elements


def generate_letterhead_pdf(
    tab_title: str,
    job_no: str,
    client: str = "",
    content_lines: list | None = None,
    content_pairs: list | None = None,
    table_rows: list | None = None,
    client_info: dict | None = None,
    force_portrait: bool = False,
    attendance_meta: dict | None = None,
    template_candidates: list | None = None,
    attendance_table: bool = False,
    job_spec_sections: list | None = None,
    total_man_days: float | None = None,
):
    """Build a PDF with ReportLab. template_candidates ignored (letterhead not embedded)."""
    del template_candidates, force_portrait

    elements = [_p(tab_title, "title")]

    if attendance_meta:
        meta = attendance_meta
        elements.append(
            _table(
                [
                    [
                        _p(
                            f"<b>Client:</b> {meta.get('client', '')}<br/>"
                            f"<b>Job:</b> {meta.get('job_no', '')}<br/>"
                            f"<b>Address:</b> {meta.get('address', '')}",
                            "body",
                        ),
                        _p(
                            f"<b>Completed Date:</b><br/>"
                            f"<b>Signature:</b> {meta.get('signature', '')}",
                            "body",
                        ),
                    ]
                ],
                col_widths=[90 * mm, 90 * mm],
                header_rows=0,
            )
        )
        elements.append(Spacer(1, 3 * mm))

    if job_spec_sections is not None:
        spec_client = {
            "client": client,
            "phone": (client_info or {}).get("phone", ""),
            "email": (client_info or {}).get("email", ""),
            "address": (client_info or {}).get("address", ""),
            "area_manager": (client_info or {}).get("area_manager", ""),
            "am_phone": (client_info or {}).get("am_phone", ""),
            "am_email": (client_info or {}).get("am_email", ""),
        }
        elements.extend(
            _render_job_spec_body(job_no, total_man_days, job_spec_sections, spec_client)
        )
    elif client_info:
        elements.append(_p(f"<b>Job Number:</b> {job_no}", "body"))
        elements.append(
            _p(
                f"<b>Client:</b> {client}  <b>Phone:</b> {client_info.get('phone', '')}  "
                f"<b>Email:</b> {client_info.get('email', '')}",
                "body",
            )
        )
        elements.append(
            _p(
                f"<b>Address:</b> {client_info.get('address', '')}  "
                f"<b>Area Manager:</b> {client_info.get('area_manager', '')}",
                "body",
            )
        )

    for line in content_lines or []:
        if str(line).strip():
            elements.append(_p(line, "body"))

    for pair_row in content_pairs or []:
        if not pair_row:
            continue
        label1, value1, label2, value2 = pair_row
        text = ""
        if str(label1).strip():
            text += f"<b>{label1}</b>{value1}"
        if str(label2).strip():
            text += f" &nbsp;&nbsp; <b>{label2}</b>{value2}"
        elements.append(_p(text, "body"))

    pagesize = A4
    if table_rows:
        headers = [str(h).strip().lower().rstrip(":") for h in table_rows[0]]
        is_attendance = attendance_table or (
            len(headers) >= 34
            and any(h.startswith("name") for h in headers)
            and "1" in headers
            and "31" in headers
        )
        if is_attendance:
            pagesize = landscape(A4)
            ncols = len(table_rows[0])
            page_w = landscape(A4)[0] - 24 * mm
            col_w = page_w / max(ncols, 1)
            elements.append(
                _table(table_rows, col_widths=[col_w] * ncols, font_size=6)
            )
        else:
            ncols = len(table_rows[0])
            page_w = A4[0] - 24 * mm
            col_w = page_w / max(ncols, 1)
            elements.append(_table(table_rows, col_widths=[col_w] * ncols))

    if attendance_meta:
        man_days_allowed = float(
            attendance_meta.get(
                "man_days_allowed", attendance_meta.get("man_days_available", 0)
            )
            or 0
        )
        elements.append(Spacer(1, 4 * mm))
        elements.append(
            _table(
                [
                    ["Man Days Allowed", f"{man_days_allowed:.1f}"],
                    ["Man Days Total", ""],
                    ["Bonus Man Days", ""],
                    ["R value of Bonus", ""],
                    ["Bonus per Man Day", ""],
                ],
                col_widths=[60 * mm, 40 * mm],
                header_rows=0,
            )
        )

    try:
        return _build_pdf(elements, pagesize=pagesize)
    except Exception as exc:
        import streamlit as st

        st.error(f"PDF generation error: {exc}")
        return None


def generate_quote_pdf(context: dict) -> BytesIO:
    """Build Tab 1 quote PDF from the same context used for Word export."""
    elements = [
        _p("Pro Paint Teams Quote", "title"),
        _p(f"<b>Quote Number:</b> {context.get('quotenumber', '')}", "body"),
        _p(f"<b>Date:</b> {context.get('quotedate', '')}", "body"),
        Spacer(1, 3 * mm),
        _p("<b>Client</b>", "heading"),
        _p(f"{context.get('clientname', '')}", "body"),
        _p(f"{context.get('clientaddress', '')}", "body"),
        _p(
            f"Phone: {context.get('clientphone', '')} | Email: {context.get('clientemail', '')}",
            "body",
        ),
        Spacer(1, 3 * mm),
        _p("<b>Area Manager</b>", "heading"),
        _p(
            f"{context.get('areaManagerName', '')} | "
            f"{context.get('areaManagerPhone', '')} | {context.get('areaManagerEmail', '')}",
            "body",
        ),
        Spacer(1, 4 * mm),
    ]

    paint_rows = [["Type", "Item", "Method", "Qty", "Class", "Material", "Labour"]]
    for spec in context.get("paint_specs") or []:
        paint_rows.append(
            [
                spec.get("type", ""),
                spec.get("item", ""),
                spec.get("method", ""),
                f"{spec.get('converted', '')} {spec.get('unit', '')}",
                spec.get("class", ""),
                spec.get("materialcost", ""),
                spec.get("labourcost", ""),
            ]
        )
    if len(paint_rows) > 1:
        elements.append(_p("<b>Paint Specifications</b>", "heading"))
        page_w = A4[0] - 24 * mm
        elements.append(
            _table(
                paint_rows,
                col_widths=[page_w / 7] * 7,
            )
        )
        elements.append(Spacer(1, 4 * mm))

    add_rows = [["Item", "Details", "Cost"]]
    for row in context.get("additional_costs") or []:
        if isinstance(row, dict):
            add_rows.append(
                [
                    row.get("description", row.get("item", "")),
                    row.get("type", row.get("details", "")),
                    row.get("amount", row.get("cost", "")),
                ]
            )
    if len(add_rows) > 1:
        elements.append(_p("<b>Additional Costs</b>", "heading"))
        page_w = A4[0] - 24 * mm
        elements.append(_table(add_rows, col_widths=[page_w / 3] * 3))
        elements.append(Spacer(1, 4 * mm))

    elements.extend(
        [
            _p(f"<b>Materials Total:</b> {context.get('materialtotal', '')}", "body"),
            _p(f"<b>Labour Total:</b> {context.get('labourtotal', '')}", "body"),
            _p(f"<b>Additional Total:</b> {context.get('additionaltotal', '')}", "body"),
            _p(f"<b>Grand Total:</b> {context.get('grandtotal', '')}", "body"),
            _p(f"<b>50% Deposit:</b> {context.get('grandtotal50', '')}", "body"),
        ]
    )
    return _build_pdf(elements)
