"""All pull-side document utilities for the McLane pipeline, in one module
(amazon_data_pipeline / core-mark_data_pipeline ``utils_data_pull.py``
design):

  1. EMAIL PARSER              — Deduction Support envelope -> DeductionSupportEmail
  2. REMITTANCE PARSER         — TIFF page 1 (OCR text) -> List[RemittanceLine]
  3. VENDOR INVOICE ADJ. FORM  — later TIFF pages (OCR text) -> VendorInvoiceAdjustmentForm
  4. GATES (SOP §3A Steps 4-8) — turn (remittance line, form) pairs into
                                 confirmed shortage records

Every field is read from OCR'd text (src/ocr_client.py) — McLane's only
source is a scanned multi-page TIFF with no text layer (SOP intro). Every
parser is deliberately lenient (missing fields are logged and counted,
never fatal) so a layout surprise degrades gracefully instead of dropping
the whole check. Validated 2026-08-14 against real fixture TIFFs (fixtures/,
git-ignored live financial documents) from the real mailbox: the email/
remittance/form-header parsers below work correctly on real OCR output; the
Adjustment Form's numeric units/amount table needed a dedicated cell-level
OCR pass (src.ocr_client.extract_adjustment_table_rows) rather than a text
regex — see that module's docstring for why.
"""

import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PIL import Image

from extraction_service.extractors.email_extractor import FetchedMessage
from extraction_service.mclane.definitions import (
    AMOUNT_TOLERANCE,
    DeductionSupportEmail,
    RemittanceLine,
    VendorInvoiceAdjustmentForm,
    VendorInvoiceAdjustmentLine,
)
from extraction_service.log import CustomLogger
from extraction_service.ocr import extract_adjustment_table_rows


logger = CustomLogger(__name__)


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def _to_iso_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    normalized = re.sub(r"\s+", " ", raw.strip())
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    logger.warning(message="Unparseable document date", fields={"raw": raw})
    return None


def _first(pattern: "re.Pattern", text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


# ===========================================================================
# 1. EMAIL PARSER (Deduction Support envelope; SOP §3A Steps 1-2)
# ===========================================================================
#
# Subject: "Deduction Support for LUCY GOODS INC - Check #: <CHECK> -
#           {AUTOSTATEMENT=...}"
# Sender:  McLaneDeductionSupport via LUCY Billing <billing@lucy.co>
# Body:    the standard document-legend + contact boilerplate (not parsed —
#          every field this pipeline needs lives on the TIFF, not the body).

DEDUCTION_SUPPORT_SUBJECT_PHRASE = "Deduction Support for LUCY GOODS INC"
DEDUCTION_SUPPORT_SENDER = "billing@lucy.co"

_TIFF_FILENAME_RE = re.compile(r"\.tiff?$", re.IGNORECASE)

_SUBJECT_RE = re.compile(
    r"Deduction Support for LUCY GOODS INC\s*-\s*Check\s*#:\s*(?P<check>\d+)\s*"
    r"-\s*\{AUTOSTATEMENT=(?P<autostatement>[^}]*)\}",
    re.IGNORECASE,
)


def is_deduction_support_message(message: FetchedMessage) -> bool:
    """True when the subject carries the Deduction Support phrase (native
    feed or a forward — matching on the phrase alone keeps forwards in the
    feed, same rationale as the sibling pipelines' subject-only match)."""
    return DEDUCTION_SUPPORT_SUBJECT_PHRASE.lower() in message.subject.lower()


def parse_deduction_support_email(message: FetchedMessage) -> DeductionSupportEmail:
    """Extract the check-level envelope from one fetched message.

    Missing subject fields are None rather than fatal — the SOP Step 1 gate
    (exactly one attachment) is checked by the caller before this is called.
    """
    subject_match = _SUBJECT_RE.search(message.subject)
    tiff_attachment = next(
        (a for a in message.attachments if _TIFF_FILENAME_RE.search(a.filename)),
        None,
    )
    parsed = DeductionSupportEmail(
        message_id=message.message_id,
        subject=message.subject,
        check_number=subject_match.group("check") if subject_match else None,
        autostatement_id=(
            subject_match.group("autostatement").strip() if subject_match else None
        ),
        tiff_filename=tiff_attachment.filename if tiff_attachment else None,
    )
    if parsed.check_number is None or parsed.tiff_filename is None:
        logger.warning(
            message="Deduction Support email missing Check # or TIFF attachment",
            fields={
                "message_id": message.message_id,
                "subject": message.subject,
                "attachment_count": len(message.attachments),
            },
        )
    return parsed


# ===========================================================================
# 2. REMITTANCE PARSER (TIFF page 1 (OCR text); SOP §3A Step 3)
# ===========================================================================
#
# Header:  "CK = <check#> <check date MM/DD/YY> <net check amount>"
# Table:   DIVISION | DIV V# | VENDOR# | CBR# | INVOICE# | INV-DATE |
#          GROSS | DISCOUNT | NET
# Footer:  "*** CHECK TOTAL ***" row — Σ NET must equal the net check amount
#          (SOP §3A Step 3 reconciliation control; advisory only here, given
#          OCR noise).

_CK_HEADER_RE = re.compile(
    r"CK\s*=\s*(?P<check>\d+)\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\s+"
    r"\$?\s?(?P<amount>-?[\d,]+\.\d{2})",
    re.IGNORECASE,
)

_CHECK_TOTAL_RE = re.compile(
    r"\*{2,3}\s*CHECK\s+TOTAL\s*\*{2,3}.*?(?P<amount>-?[\d,]+\.\d{2})",
    re.IGNORECASE,
)

# One remittance row, tokenized on whitespace and matched from the RIGHT
# (pinned against real OCR'd fixture pages, 2026-06/07 batch): DIVISION is
# variable-width — 1 token ("BLUEGRASS", "WESTERN") or 2 ("MCLANE OCALA",
# "MCLANE OHIO") — so a fixed left-to-right column regex silently drops
# every 2-word-division row. The trailing 8 tokens (DIV V# | VENDOR# | CBR# |
# INVOICE# | INV-DATE | GROSS | DISCOUNT | NET) are fixed-width instead, and
# DISCOUNT prints as a bare "00" (no decimal point) when zero — a decimal-
# point-required money pattern silently drops every …C1 line, since McLane
# never discounts a deduction (SOP §1). ``_MONEY_TOKEN_RE`` accepts both
# shapes.
_MONEY_TOKEN_RE = re.compile(r"^-?\$?[\d,]+(?:\.\d{1,2})?$")
_DATE_TOKEN_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

_SKIP_LINE_MARKERS = ("DIVISION", "DIV V#", "CHECK TOTAL", "INVOICE#")


def parse_check_header(remittance_page_text: str) -> Tuple[Optional[str], Optional[float]]:
    """Extract (check_date ISO, net_check_amount) from the CK= header line."""
    match = _CK_HEADER_RE.search(remittance_page_text)
    if not match:
        logger.warning(message="Remittance CK= header not found")
        return None, None
    return _to_iso_date(match.group("date")), _to_float(match.group("amount"))


def parse_remittance_lines(remittance_page_text: str) -> List[RemittanceLine]:
    """Parse every document row of the remittance table (SOP §3A Step 3).

    Column-header and ``*** CHECK TOTAL ***`` rows are skipped by marker
    match before the row ever reaches the tokenizer, so the control total is
    never mistaken for a document line. Rows are matched from the right
    (see ``_MONEY_TOKEN_RE`` / ``_DATE_TOKEN_RE`` above) so a variable-width
    DIVISION never shifts the fixed-width columns.
    """
    lines: List[RemittanceLine] = []
    for raw_line in remittance_page_text.splitlines():
        if any(marker in raw_line.upper() for marker in _SKIP_LINE_MARKERS):
            continue
        tokens = raw_line.split()
        # DIVISION (>=1 token) + DIV V# + VENDOR# + CBR# + INVOICE# +
        # INV-DATE + GROSS + DISCOUNT + NET = >=9 tokens.
        if len(tokens) < 9:
            continue
        (
            div_v_tok, vendor_tok, cbr_tok, invoice_tok, date_tok,
            gross_tok, discount_tok, net_tok,
        ) = tokens[-8:]
        division_tokens = tokens[:-8]
        if not (
            _DATE_TOKEN_RE.match(date_tok)
            and _MONEY_TOKEN_RE.match(gross_tok)
            and _MONEY_TOKEN_RE.match(discount_tok)
            and _MONEY_TOKEN_RE.match(net_tok)
        ):
            continue
        lines.append(
            RemittanceLine(
                division=" ".join(division_tokens),
                div_v_number=div_v_tok,
                vendor_number=vendor_tok,
                cbr_number=cbr_tok,
                invoice_number=invoice_tok,
                invoice_date=_to_iso_date(date_tok),
                gross=_to_float(gross_tok),
                discount=_to_float(discount_tok),
                net=_to_float(net_tok),
            )
        )
    if not lines:
        logger.warning(
            message="No remittance lines parsed from page 1 — layout drift "
            "or a poor OCR pass",
        )
    return lines


def check_total_reconciles(
    remittance_page_text: str, lines: List[RemittanceLine], net_check_amount: Optional[float]
) -> bool:
    """SOP §3A Step 3 control: Σ NET across every parsed line should equal
    both the printed *** CHECK TOTAL *** row and the CK= header amount.
    Advisory only (logged, never fatal) — OCR noise on any one row would
    otherwise block an entire check's worth of otherwise-good records."""
    printed_total = _first(_CHECK_TOTAL_RE, remittance_page_text)
    printed_total_value = _to_float(printed_total) if printed_total else None
    lines_total = round(sum(line.net or 0.0 for line in lines), 2)
    target = printed_total_value if printed_total_value is not None else net_check_amount
    if target is None:
        return False
    ok = abs(lines_total - target) <= max(AMOUNT_TOLERANCE * len(lines), AMOUNT_TOLERANCE)
    if not ok:
        logger.warning(
            message="Remittance Σ NET does not reconcile to the check total (advisory)",
            fields={"lines_total": lines_total, "target": target},
        )
    return ok


# ===========================================================================
# 3. VENDOR INVOICE ADJUSTMENT FORM (later TIFF pages (OCR text); SOP §3A
#    Steps 6-8)
# ===========================================================================

_FORM_HEADER_RE = re.compile(r"Vendor\s+Invoice\s+Adjustment\s+Form", re.IGNORECASE)
_INVOICE_ADJ_RE = re.compile(
    r"Invoice\s*Adj\s*#\s*:?\s*(?P<adj>(?:13|14|15|16)\d+C\d)", re.IGNORECASE
)
# "Invoice #: 14210" — must not swallow the …C1 adjustment number above it.
_FORM_INVOICE_RE = re.compile(
    r"Invoice\s*#\s*:?\s*(?P<invoice>(?:13|14|15|16)\d+)(?!\s*C\d)", re.IGNORECASE
)
_FORM_DATE_RE = re.compile(r"^\s*Date\s*:?\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE | re.MULTILINE)
_PROCESS_INVOICE_DATE_RE = re.compile(
    r"process\s+this\s+invoice\s+dated\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
# Real documents print the division as the page's own header line
# ("BLUEGRASS Report Date: Jun 20, 2026", "MCLANE OCALA Report Date: ...")
# rather than a "Division:" label — the label form is kept as a fallback in
# case a division ever prints it (no live example seen yet).
_FORM_DIVISION_HEADER_RE = re.compile(
    r"^\s*(?P<division>.+?)\s+Report\s+Date\s*:", re.IGNORECASE | re.MULTILINE
)
_FORM_DIVISION_LABEL_RE = re.compile(r"Division\s*:?\s*(?P<division>\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_REASON_RE = re.compile(
    r"(?P<reason>ITEM BILLED BUT NOT ORDERED OR ZERO UNITS RECEIVED\.?"
    r"|REC'?D SOME BUT NOT ALL[^\n]*)",
    re.IGNORECASE,
)
_TOTAL_FOR_ADJUSTMENT_RE = re.compile(
    r"Total\s+for\s+Adjustment\s*:?\s*\$?\s?(?P<amount>-?[\d,]+\.\d{2})",
    re.IGNORECASE,
)

# One PO/item line row, 9 columns (pinned against the real form,
# fixtures/8500120866.tif page 1 — NOT the 5-column shape the SOP's prose
# field list implies): GTIN | UIN | Units Billed | Units Received |
# Units In Dispute | Billed Amount | Adjusted Billed Amount |
# Unit Amt In Dispute | Amount Adjusted. GTIN/UIN print as "00000" (SOP §10:
# no usable UPC) and OCR into unrecognizable garbage on this table regardless
# of PSM mode or the pdfplumber-layout pipeline (src/ocr_client.py) — a
# Tesseract character-recognition issue on this specific table's rendering,
# confirmed 2026-08-14. So the row is matched from the RIGHT (last 7
# numeric-shaped tokens), same technique as the remittance parser, ignoring
# whatever garbage precedes them; "Adjusted Billed Amount" and "Unit Amt In
# Dispute" are captured but currently unused (they were 292.50 / 0.00 —
# redundant with Billed Amount — on every observed sample). Reason/
# description are captured independently of this row (see
# _split_po_blocks) so a still-unreadable row never loses the fields around it.
_ADJ_LINE_NUMERIC_TOKEN_RE = re.compile(r"^-?\$?[\d,]+(?:\.\d{1,2})?$")


def _parse_adjustment_line_numbers(block: str) -> Optional[dict]:
    for raw_line in block.splitlines():
        tokens = raw_line.split()
        if len(tokens) < 7:
            continue
        trailing = tokens[-7:]
        if not all(_ADJ_LINE_NUMERIC_TOKEN_RE.match(t) for t in trailing):
            continue
        billed, received, dispute, billed_amount, _adj_billed, _unit_dispute, amount = trailing
        return {
            "units_billed": _to_float(billed),
            "units_received": _to_float(received),
            "units_in_dispute": _to_float(dispute),
            "unit_cost": _to_float(billed_amount),
            "amount_adjusted": _to_float(amount),
        }
    return None

# Each PO/item line on the form repeats its own "McLane PO Number: ..."
# header (SOP Appendix A's 14199C1 sample prints it 3 times, once per PO
# line) — splitting on it gives one chunk per line, independent of whether
# the trailing numeric row OCRs.
_PO_BLOCK_HEADER_RE = re.compile(r"McLane\s+PO\s+Number\s*:", re.IGNORECASE)


def _split_po_blocks(page_text: str) -> List[str]:
    """Split a form page into per-PO-line chunks, each starting at its own
    "McLane PO Number: ..." header."""
    indices = [m.start() for m in _PO_BLOCK_HEADER_RE.finditer(page_text)]
    if not indices:
        return []
    indices.append(len(page_text))
    return [page_text[indices[i] : indices[i + 1]] for i in range(len(indices) - 1)]


def _block_item_description(block: str) -> Optional[str]:
    """The item description is the first non-empty line after the PO
    Number/Date line that isn't itself the reason line."""
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    for line in lines[1:]:
        if _REASON_RE.search(line):
            break
        return line
    return None


def find_form_pages(page_texts: List[str]) -> List[int]:
    """Indices of every TIFF page whose header reads "Vendor Invoice
    Adjustment Form" (SOP §3A Step 6)."""
    return [i for i, text in enumerate(page_texts) if _FORM_HEADER_RE.search(text)]


def parse_adjustment_form_page(
    page_text: str,
    source_filename: str,
    page_index: int,
    page_image: Optional[Image.Image] = None,
) -> Optional[VendorInvoiceAdjustmentForm]:
    """Parse one "Vendor Invoice Adjustment Form" page into a validated
    model, or None when no Invoice Adj# can be read (a missing/unreadable
    form is a request-from-McLane case, SOP §3A Step 6 — never fabricated).

    ``page_image`` (optional): when supplied, each block's numeric row is
    read via cell-level OCR (src.ocr_client.extract_adjustment_table_rows —
    reliable) instead of the text-regex fallback (_parse_adjustment_line_numbers
    — a best-effort match that rarely succeeds on this specific table; see
    src/ocr_client.py's docstring for why). Text-only callers (e.g. tests
    feeding synthetic OCR text with no source image) still work via the
    fallback.
    """
    adj_match = _INVOICE_ADJ_RE.search(page_text)
    if not adj_match:
        logger.warning(
            message="Vendor Invoice Adjustment Form page has no readable "
            "Invoice Adj# — treating as a missing-doc case",
            fields={"source_filename": source_filename, "page_index": page_index},
        )
        return None

    invoice_match = _FORM_INVOICE_RE.search(page_text)
    date_match = _FORM_DATE_RE.search(page_text)
    process_date_match = _PROCESS_INVOICE_DATE_RE.search(page_text)
    division_match = _FORM_DIVISION_HEADER_RE.search(page_text) or _FORM_DIVISION_LABEL_RE.search(page_text)
    total_match = _TOTAL_FOR_ADJUSTMENT_RE.search(page_text)

    cell_ocr_rows: List[Optional[dict]] = []
    if page_image is not None:
        try:
            cell_ocr_rows = extract_adjustment_table_rows(page_image)
        except Exception as e:
            logger.warning(
                message="Cell-level table OCR failed — falling back to text regex",
                fields={"source_filename": source_filename, "error": str(e)},
            )

    blocks = _split_po_blocks(page_text)
    lines: List[VendorInvoiceAdjustmentLine] = []
    for block_index, block in enumerate(blocks):
        reason_match = _REASON_RE.search(block)
        reason_text = reason_match.group("reason").strip() if reason_match else None

        # Cell-level OCR first (reliable — see module docstring); fall back
        # to the text-regex match (rarely succeeds on this table, but free
        # to try) when unavailable or the grid wasn't found for this block.
        cell_numbers = (
            cell_ocr_rows[block_index]
            if block_index < len(cell_ocr_rows) and cell_ocr_rows[block_index]
            else None
        )
        numbers = cell_numbers or _parse_adjustment_line_numbers(block) or {}

        units_received = numbers.get("units_received")
        if units_received is None and reason_text and "ZERO UNITS RECEIVED" in reason_text.upper():
            # Text-corroborated inference, not a guess: the reason line
            # itself states this in words (SOP: "in every observed case
            # Units Received = 0") — used only when the cell/regex OCR of
            # the number itself came back empty.
            units_received = 0.0

        units_billed = numbers.get("units_billed")
        units_in_dispute = numbers.get("units_in_dispute")
        if units_billed is None and units_received is not None and units_in_dispute is not None:
            # Field-identity inference, not a guess: the form's own three
            # unit columns are billed = received + in-dispute by definition
            # (SOP §2's field list) — used only when Billed specifically
            # failed to OCR but its two components both did.
            units_billed = units_received + units_in_dispute

        lines.append(
            VendorInvoiceAdjustmentLine(
                item_description=_block_item_description(block),
                reason=reason_text,
                units_billed=units_billed,
                units_received=units_received,
                units_in_dispute=units_in_dispute,
                unit_cost=numbers.get("unit_cost"),
                amount_adjusted=numbers.get("amount_adjusted"),
            )
        )

    form = VendorInvoiceAdjustmentForm(
        source_filename=source_filename,
        page_index=page_index,
        invoice_number=(
            invoice_match.group("invoice")
            if invoice_match
            else re.sub(r"C\d$", "", adj_match.group("adj"))
        ),
        invoice_adj_number=adj_match.group("adj"),
        form_date=_to_iso_date(date_match.group("date")) if date_match else None,
        invoice_date=(
            _to_iso_date(process_date_match.group("date")) if process_date_match else None
        ),
        division=division_match.group("division") if division_match else None,
        lines=lines,
        total_for_adjustment=_to_float(total_match.group("amount")) if total_match else None,
    )
    if not lines:
        logger.warning(
            message="No PO/item lines parsed on a Vendor Invoice Adjustment "
            "Form page",
            fields={"invoice_adj_number": form.invoice_adj_number},
        )
    return form


def parse_adjustment_forms(
    page_texts: List[str],
    source_filename: str,
    page_images: Optional[List[Image.Image]] = None,
) -> Dict[str, VendorInvoiceAdjustmentForm]:
    """Parse every Vendor Invoice Adjustment Form page in the TIFF, keyed by
    Invoice Adj# (…C1). A duplicate key (a form spanning >1 page) keeps the
    first page parsed and is logged — merging multi-page forms is a known
    gap, flagged rather than silently dropping the continuation page.

    ``page_images`` (optional, same length/order as ``page_texts``): enables
    cell-level OCR of each form's numeric table (see
    parse_adjustment_form_page).
    """
    forms: Dict[str, VendorInvoiceAdjustmentForm] = {}
    for page_index in find_form_pages(page_texts):
        page_image = (
            page_images[page_index]
            if page_images is not None and page_index < len(page_images)
            else None
        )
        form = parse_adjustment_form_page(
            page_texts[page_index], source_filename, page_index, page_image
        )
        if form is None:
            continue
        if form.invoice_adj_number in forms:
            logger.warning(
                message="Duplicate Invoice Adj# across form pages — possible "
                "multi-page form; keeping the first page only",
                fields={"invoice_adj_number": form.invoice_adj_number},
            )
            continue
        forms[form.invoice_adj_number] = form
    return forms


# ===========================================================================
# 4. GATES (SOP §3A Steps 4-8): remittance line + form -> confirmed shortage
# ===========================================================================


class ShortageGateStats(dict):
    """Run-stats counters for the SOP §3A gate pipeline, as a plain dict
    with named keys the caller merges into its overall stats."""

    def __init__(self) -> None:
        super().__init__(
            shortage_candidates=0,
            missing_forms=0,
            gate_c_failures=0,
            gate_d_failures=0,
            confirmed_shortages=0,
        )


def build_shortage_records(
    remittance_lines: List[RemittanceLine],
    forms: Dict[str, VendorInvoiceAdjustmentForm],
    stats: Dict,
) -> List[Tuple[RemittanceLine, VendorInvoiceAdjustmentForm]]:
    """Run every …C1 remittance line through Gates B (already applied via
    ``RemittanceLine.is_shortage_candidate``), C (quantity variance), and D
    (dollar reconciliation), returning only records that survive all three
    (SOP §3A Steps 5-8). A record failing any gate is logged and counted,
    never silently dropped.
    """
    confirmed: List[Tuple[RemittanceLine, VendorInvoiceAdjustmentForm]] = []
    for line in remittance_lines:
        if not line.is_shortage_candidate:
            continue
        stats["shortage_candidates"] = stats.get("shortage_candidates", 0) + 1

        form = forms.get(line.invoice_number)
        if form is None:
            stats["missing_forms"] = stats.get("missing_forms", 0) + 1
            logger.warning(
                message="No Vendor Invoice Adjustment Form found for a …C1 "
                "line — missing-doc case (SOP §3A Step 6): request from "
                "cap.research@mclaneco.com",
                fields={"invoice_adj_number": line.invoice_number},
            )
            continue

        if not form.is_quantity_variance:
            stats["gate_c_failures"] = stats.get("gate_c_failures", 0) + 1
            logger.warning(
                message="Gate C failed — not a quantity-variance reason, or "
                "Units Received >= Units Billed",
                fields={"invoice_adj_number": line.invoice_number},
            )
            continue

        if not form.totals_consistent(remittance_net=line.net):
            if form.dollars_corroborated(line.net):
                # SOP §3A Step 8: a per-line dollar cell misOCR'd, but
                # exactly one combination of the per-line candidate readings
                # ties the remittance NET — an independent reading — so the
                # deduction is confirmed (the row's Invoice Amount is built
                # from the remittance NET, not the corrupted cell).
                logger.warning(
                    message="Gate D passed via candidate-reconciliation "
                    "fallback — a per-line dollar cell misOCR'd; dollars "
                    "corroborated by the remittance NET",
                    fields={
                        "invoice_adj_number": line.invoice_number,
                        "lines_total": form.lines_total(),
                        "remittance_net": line.net,
                    },
                )
            else:
                stats["gate_d_failures"] = stats.get("gate_d_failures", 0) + 1
                logger.warning(
                    message="Gate D failed — dollars do not reconcile "
                    "(Σ Amount Adjusted vs. Total for Adjustment vs. remittance NET)",
                    fields={
                        "invoice_adj_number": line.invoice_number,
                        "lines_total": form.lines_total(),
                        "total_for_adjustment": form.total_for_adjustment,
                        "remittance_net": line.net,
                    },
                )
                continue

        stats["confirmed_shortages"] = stats.get("confirmed_shortages", 0) + 1
        confirmed.append((line, form))
    return confirmed
