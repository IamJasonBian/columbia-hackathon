"""Normalize extracted PDF table rows into RFQ line items."""

from __future__ import annotations

import re
from typing import Any, Literal

from services.extraction.models import ExtractedDocument, ExtractedTable

RfqLineFormat = Literal["numbered", "spec_ref", "description_part"]

_QTY_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")
_REQUIRED_RE = re.compile(r"^([\d,]+(?:\.\d+)?)\s+(.+)$", re.IGNORECASE)


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lower())


def _parse_qty(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or not _QTY_RE.match(s):
        return None
    return float(s)


def _detect_format(headers: list[str]) -> RfqLineFormat | None:
    norm = {_norm_header(h) for h in headers}
    if "item / specification" in norm or "item/specification" in norm:
        return "spec_ref"
    if any(h in norm for h in ("item no.", "item no")) and any(
        x in norm for x in ("item description", "description")
    ):
        return "numbered"
    if "description" in norm and any(
        x in norm
        for x in (
            "mfr. part #",
            "mfr part #",
            "part no.",
            "part no",
            "catalogue no.",
            "catalogue no",
        )
    ):
        return "description_part"
    return None


def _col_index(headers: list[str], *candidates: str) -> int | None:
    norm = [_norm_header(h) for h in headers]
    for cand in candidates:
        c = _norm_header(cand)
        if c in norm:
            return norm.index(c)
    return None


def _cell(row: list[Any], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    val = row[idx]
    return "" if val is None else str(val).strip()


def _option_flag(description: str) -> bool:
    return description.upper().startswith("OPTION:") or description.upper().startswith("OPTION ")


def normalize_table(
    table: ExtractedTable,
    *,
    attachment: str = "",
) -> list[dict[str, Any]]:
    """Turn one extracted table into normalized line-item dicts."""
    fmt = _detect_format(table.headers)
    if fmt is None:
        return []

    def _split_qty_uom(raw: str) -> tuple[float | None, str | None, str | None]:
        if not raw:
            return None, None, None
        m = _REQUIRED_RE.match(raw.strip())
        if m:
            return _parse_qty(m.group(1)), m.group(2).strip() or None, raw
        return _parse_qty(raw), None, raw

    lines: list[dict[str, Any]] = []
    if fmt == "description_part":
        desc = _col_index(table.headers, "Description", "Item Description")
        part = _col_index(
            table.headers,
            "Mfr. Part #",
            "Mfr Part #",
            "Part No.",
            "Part No",
            "Catalogue No.",
        )
        qty = _col_index(table.headers, "Quantity", "Req. Qty")
        price = _col_index(table.headers, "Unit Price", "Price / Unit")
        delivery = _col_index(table.headers, "Delivery")

        for seq, row in enumerate(table.rows, start=1):
            description = _cell(row, desc)
            if not description:
                continue
            q, uom, req_raw = _split_qty_uom(_cell(row, qty))
            lines.append(
                {
                    "line_seq": seq,
                    "line_no": seq,
                    "rfq_format": "description_part",
                    "buyer_part_no": _cell(row, part) or None,
                    "description": description,
                    "quantity": q,
                    "unit_of_measure": uom,
                    "unit_price": _parse_qty(_cell(row, price)),
                    "delivery": _cell(row, delivery) or None,
                    "is_option": _option_flag(description),
                    "required_qty_raw": req_raw,
                    "source_attachment": attachment,
                    "source_page": table.page,
                    "source_table": table.name,
                    "raw_row": dict(zip(table.headers, row)),
                }
            )
        return lines

    if fmt == "numbered":
        i_no = _col_index(table.headers, "Item No.", "Item No")
        part = _col_index(
            table.headers,
            "Part No.",
            "Part No",
            "Catalogue No.",
            "Catalogue No",
        )
        desc = _col_index(table.headers, "Item Description", "Description")
        qty = _col_index(table.headers, "Quantity", "Req. Qty", "Req Qty")
        uom = _col_index(table.headers, "UoM", "Unit of Measure")
        price = _col_index(table.headers, "Unit Price", "Price / Unit")
        delivery = _col_index(table.headers, "Delivery")

        for seq, row in enumerate(table.rows, start=1):
            description = _cell(row, desc)
            if not description:
                continue
            line_no_raw = _cell(row, i_no)
            line_no = int(line_no_raw) if line_no_raw.isdigit() else None
            lines.append(
                {
                    "line_seq": seq,
                    "line_no": line_no,
                    "rfq_format": "numbered",
                    "buyer_part_no": _cell(row, part) or None,
                    "description": description,
                    "quantity": _parse_qty(_cell(row, qty)),
                    "unit_of_measure": _cell(row, uom) or None,
                    "unit_price": _parse_qty(_cell(row, price)),
                    "delivery": _cell(row, delivery) or None,
                    "is_option": _option_flag(description),
                    "required_qty_raw": None,
                    "source_attachment": attachment,
                    "source_page": table.page,
                    "source_table": table.name,
                    "raw_row": dict(zip(table.headers, row)),
                }
            )
        return lines

    # spec_ref
    spec = _col_index(table.headers, "Item / Specification", "Item/Specification")
    ref = _col_index(table.headers, "Ref.")
    req = _col_index(table.headers, "Required")
    price = _col_index(table.headers, "Price / Unit")

    for seq, row in enumerate(table.rows, start=1):
        description = _cell(row, spec)
        if not description:
            continue
        required_raw = _cell(row, req)
        quantity: float | None = None
        uom: str | None = None
        if required_raw:
            m = _REQUIRED_RE.match(required_raw)
            if m:
                quantity = _parse_qty(m.group(1))
                uom = m.group(2).strip() or None
            else:
                quantity = _parse_qty(required_raw)
        lines.append(
            {
                "line_seq": seq,
                "line_no": seq,
                "rfq_format": "spec_ref",
                "buyer_part_no": _cell(row, ref) or None,
                "description": description,
                "quantity": quantity,
                "unit_of_measure": uom,
                "unit_price": _parse_qty(_cell(row, price)),
                "delivery": None,
                "is_option": _option_flag(description),
                "required_qty_raw": required_raw or None,
                "source_attachment": attachment,
                "source_page": table.page,
                "source_table": table.name,
                "raw_row": dict(zip(table.headers, row)),
            }
        )
    return lines


def extract_lines_from_document(doc: ExtractedDocument) -> list[dict[str, Any]]:
    """All line items from email + attachment tables (deduped by line_no + description)."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []

    def walk(d: ExtractedDocument, attachment_path: str) -> None:
        for table in d.tables:
            for line in normalize_table(table, attachment=attachment_path):
                key = (
                    line.get("line_no"),
                    line.get("description"),
                    line.get("buyer_part_no"),
                    line.get("source_page"),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(line)
        for att in d.attachments:
            child = f"{attachment_path}/{att.filename}" if attachment_path else att.filename
            walk(att, child)

    walk(doc, "")
    out.sort(
        key=lambda x: (
            x.get("source_page") or 0,
            x.get("line_no") or 9999,
            x.get("line_seq", 0),
        )
    )
    for seq, line in enumerate(out, start=1):
        line["line_seq"] = seq
    return out


_RFQ_ID_RE = re.compile(r"RFQ[-\s]?([A-Z]{3}-\d+)", re.IGNORECASE)


def rfq_number_from_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    m = _RFQ_ID_RE.search(subject)
    return m.group(1).upper() if m else None
