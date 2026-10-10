"""Pydantic data models for McLane shortage deduction records.

These models mirror the McLane Shortage Retrieval Mapping SOP:
  - DeductionSupportEmail comes from the "Deduction Support for LUCY GOODS
    INC" email (sender McLaneDeductionSupport via LUCY Billing
    <billing@lucy.co>); its subject carries the Check # + AUTOSTATEMENT id,
    and it carries exactly one attachment: a multi-page scanned TIFF
    (SOP §3A Step 1).
  - RemittanceLine is one row of the remittance advice (TIFF page 1, SOP
    §3A Step 3): every document on the check, with GROSS/DISCOUNT/NET.
    ``classification`` applies the Gate A (deduction vs. paid invoice) and
    Gate B (INVOICE# pattern) rules from SOP §3A Steps 4-5.
  - VendorInvoiceAdjustmentForm / VendorInvoiceAdjustmentLine model the
    per-…C1 backup page (SOP §3A Step 6): billed vs. received units, $.
  - McLaneShortageRecord is the flat output record for one confirmed
    shortage (the hackathon stand-in for upstream's McLaneAirtableRow —
    same fields, no Airtable aliases or sync concerns).

Ported from mclane_data_pipeline/src/definitions.py; Airtable/BrandConfig
models are intentionally left behind.

Grain: ONE Airtable row per confirmed …C1 Vendor Invoice Adjustment (the
adjustment's own Invoice Adj# is its unique ID — SOP §3A Step 9). A line only
reaches this grain after surviving every gate in SOP §3A (Gate A: deduction,
not a paid invoice; Gate B: …C1 pattern, not billback/memo/separately-mailed;
Gate C: quantity variance, not price/allowance; Gate D: dollars reconcile).

Every field below is read via OCR of a scanned TIFF (src/ocr_client.py) —
there is no text-layer alternative (SOP intro). OCR noise means these models
are deliberately lenient (Optional almost everywhere): a field that failed to
OCR does not block the record, it is surfaced in the run stats instead.
"""

import re
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


# Distributor tag stamped on every output record.
DISTRIBUTOR_NAME = "McLane"

# Dollar-tie-out tolerance (OCR'd digits, so kept a bit looser than the
# text-layer pipelines' $0.02).
AMOUNT_TOLERANCE = 0.05

# McLane will not research a disputed amount at or below this threshold
# (SOP §7 "Outstanding Items" — sourced from a 2015 vendor guide; advisory
# only, never used to drop a record).
MIN_DISPUTABLE_AMOUNT = 50.00

# Per the brand's trade agreement (SOP §7): the informal number to plan
# against until confirmed with McLane Central Trade Payables (SOP §6 — no
# window is actually published in these emails).
SHORTAGE_DISPUTE_WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# Gate B: INVOICE# pattern -> classification (SOP §3A Step 5 table)
# ---------------------------------------------------------------------------

class LineClassification(str, Enum):
    """Remittance-line classification by INVOICE# pattern + NET sign.

    Only VENDOR_INVOICE_ADJUSTMENT (a negative-NET …C1 line) ever advances
    past Gate B into the shortage flow (SOP §3A Step 5).
    """

    VENDOR_INVOICE_ADJUSTMENT = "vendor_invoice_adjustment"  # (14|15|16)#C# — shortage candidate
    MERCHANDISE_INVOICE = "merchandise_invoice"              # positive NET — invoice McLane is paying
    TRADE_BILLBACK = "trade_billback"                        # B<division># — contractual, exclude
    DEBIT_MEMO = "debit_memo"                                # DM<XX># — compliance/other, case-by-case
    CREDIT_MEMO = "credit_memo"                               # CM... — vendor-issued, no copy provided
    CREDIT_DOC_SEPARATE = "credit_doc_separate"                # AF... / bare 14|15|16 — mailed separately
    UNKNOWN = "unknown"


# Division codes observed on B<code># trade/promo billback docs (SOP §1);
# "etc." in the SOP — the pattern below accepts any 2-letter code and this
# set is only used to flag a genuinely new one for a human (never to reject).
KNOWN_TRADE_DIVISION_CODES = {"MO", "SW", "NW", "HP", "NT", "SO"}

# The credit-doc series is 13xxx-16xxx (SOP: "13xxx in March, 14xxx in
# Jun-Jul" — same family, the leading digit rolls with time/volume).
_SHORTAGE_RE = re.compile(r"^(?:13|14|15|16)\d+C\d$")
_BARE_CREDIT_RE = re.compile(r"^(?:13|14|15|16)\d+$")
_AF_RE = re.compile(r"^AF\d*$")
_TRADE_BILLBACK_RE = re.compile(r"^B([A-Z]{2})\d+$")
_DEBIT_MEMO_RE = re.compile(r"^DM[A-Z]{2}")
_CREDIT_MEMO_RE = re.compile(r"^CM")


def classify_remittance_line(
    invoice_number: str, net: Optional[float]
) -> LineClassification:
    """Gate A (deduction vs. paid invoice) + Gate B (INVOICE# pattern).

    Gate A first: a positive NET is McLane paying the base merchandise
    invoice, never a deduction — Gate B is only meaningful on negative-NET
    rows (SOP §3A Step 4). Order inside Gate B matters: the bare/AF credit-
    doc-mailed-separately shape must be checked before the general trade
    pattern so it is never mis-tagged.
    """
    number = (invoice_number or "").strip().upper()
    if net is not None and net > 0:
        return LineClassification.MERCHANDISE_INVOICE
    if _SHORTAGE_RE.match(number):
        return LineClassification.VENDOR_INVOICE_ADJUSTMENT
    if _BARE_CREDIT_RE.match(number) or _AF_RE.match(number):
        return LineClassification.CREDIT_DOC_SEPARATE
    if _TRADE_BILLBACK_RE.match(number):
        return LineClassification.TRADE_BILLBACK
    if _DEBIT_MEMO_RE.match(number):
        return LineClassification.DEBIT_MEMO
    if _CREDIT_MEMO_RE.match(number):
        return LineClassification.CREDIT_MEMO
    return LineClassification.UNKNOWN


# ---------------------------------------------------------------------------
# Deduction Support email (envelope; SOP §2, §3A Steps 1-2)
# ---------------------------------------------------------------------------

class DeductionSupportEmail(BaseModel):
    """One "Deduction Support for LUCY GOODS INC" email == one check.

    ``check_date`` and ``net_check_amount`` come off the remittance header's
    ``CK = <check#> <date> <amount>`` line (TIFF page 1), not the subject —
    the subject only carries the check number + AUTOSTATEMENT id.
    """

    message_id: str = Field(description="Gmail message ID (ingestion cursor)")
    subject: str
    check_number: Optional[str] = Field(
        None, description="From the subject: 'Check #: <CHECK>'"
    )
    autostatement_id: Optional[str] = Field(
        None,
        description="McLane's internal statement/batch reference, from the "
        "subject's '{AUTOSTATEMENT=...}' suffix",
    )
    check_date: Optional[str] = Field(
        None,
        description="ISO date from the remittance header CK= line — the "
        "dispute-clock start (SOP §6)",
    )
    net_check_amount: Optional[float] = Field(
        None, description="Third value on the remittance header CK= line"
    )
    tiff_filename: Optional[str] = Field(
        None,
        description="'Rendered Autodeduction Check - <M-D-YYYY> - LUCY "
        "GOODS INC - <CHECK>.tif' — the packet's single attachment",
    )


# ---------------------------------------------------------------------------
# Remittance advice line (TIFF page 1; SOP §3A Step 3)
# ---------------------------------------------------------------------------

class RemittanceLine(BaseModel):
    """One row of the remittance table: DIVISION | DIV V# | VENDOR# | CBR# |
    INVOICE# | INV-DATE | GROSS | DISCOUNT | NET."""

    division: Optional[str] = None
    div_v_number: Optional[str] = None
    vendor_number: Optional[str] = None
    cbr_number: Optional[str] = None
    invoice_number: str = Field(description="e.g. 14210C1, BMO1234, DMAB5678")
    invoice_date: Optional[str] = Field(None, description="ISO YYYY-MM-DD")
    gross: Optional[float] = None
    discount: Optional[float] = Field(
        None, description="Terms/early-pay discount — never disputed (SOP §1)"
    )
    net: Optional[float] = Field(
        None, description="Negative = deduction; positive = invoice paid"
    )

    @field_validator("invoice_number", mode="before")
    @classmethod
    def _normalize_invoice_number(cls, v: object) -> str:
        """Strip anything non-alphanumeric before classification.

        Real INVOICE# values are always pure alphanumeric (digits, B+letters,
        DM+letters, CM, AF — never punctuation), but OCR occasionally inserts
        a stray symbol (confirmed 2026-08-14: "13443C1" read as "13443¢C1",
        a spurious cent sign) that would otherwise make an exact-shape
        classification regex miss a real shortage line entirely.
        """
        return re.sub(r"[^A-Za-z0-9]", "", str(v)).strip().upper()

    @property
    def classification(self) -> LineClassification:
        return classify_remittance_line(self.invoice_number, self.net)

    @property
    def is_shortage_candidate(self) -> bool:
        return self.classification == LineClassification.VENDOR_INVOICE_ADJUSTMENT

    @property
    def base_invoice_number(self) -> Optional[str]:
        """The …C1 suffix stripped — e.g. '14210C1' -> '14210' (SOP §1: the
        base positive line on the same remittance this adjustment offsets)."""
        if not self.is_shortage_candidate:
            return None
        return re.sub(r"C\d$", "", self.invoice_number)


# ---------------------------------------------------------------------------
# Vendor Invoice Adjustment Form (TIFF backup page; SOP §3A Steps 6-8)
# ---------------------------------------------------------------------------

# Verbatim reason observed on every live sample (SOP §1, §3A Step 7). Other
# reasons are advisory-only — matched loosely, never used to hard-fail.
_QUANTITY_VARIANCE_REASON_RE = re.compile(
    r"ITEM\s+BILLED\s+BUT\s+NOT\s+ORDERED\s+OR\s+ZERO\s+UNITS\s+RECEIVED"
    r"|REC'?D\s+SOME\s+BUT\s+NOT\s+ALL",
    re.IGNORECASE,
)


class VendorInvoiceAdjustmentLine(BaseModel):
    """One PO/item line within a Vendor Invoice Adjustment Form."""

    item_description: Optional[str] = None
    reason: Optional[str] = Field(
        None, description="Printed reason text (SOP §1 / §3A Step 7)"
    )
    units_billed: Optional[float] = None
    units_received: Optional[float] = Field(
        None, description="Observed 0 in every live sample (SOP §1)"
    )
    units_in_dispute: Optional[float] = None
    unit_cost: Optional[float] = Field(None, description="Billed Amount / unit")
    amount_adjusted: Optional[float] = Field(
        None, description="= Units In Dispute × unit cost (Gate D)"
    )

    @property
    def is_quantity_variance(self) -> bool:
        """Gate C: a receiving/quantity-variance reason AND
        Units Received < Units Billed (SOP §3A Step 7)."""
        if not self.reason or not _QUANTITY_VARIANCE_REASON_RE.search(self.reason):
            return False
        if self.units_billed is None or self.units_received is None:
            return False
        return self.units_received < self.units_billed

    def amount_consistent(self) -> bool:
        """Gate D line check: |Amount Adjusted| = Units In Dispute × unit
        cost. Compared by magnitude — real forms print Amount Adjusted as a
        negative credit (e.g. "-1,462.50", confirmed 2026-08-14 against
        fixtures/8500120866.tif) while Units In Dispute × unit cost is a
        positive product; a signed comparison would fail every real line."""
        if self.units_in_dispute is None or self.unit_cost is None or self.amount_adjusted is None:
            return False
        return (
            abs(abs(self.units_in_dispute * self.unit_cost) - abs(self.amount_adjusted))
            <= AMOUNT_TOLERANCE
        )


class VendorInvoiceAdjustmentForm(BaseModel):
    """One parsed "Vendor Invoice Adjustment Form" backup page (or set of
    lines for one Invoice Adj#, if it spans multiple TIFF pages)."""

    source_filename: str = Field(description="The packet's TIFF filename")
    page_index: Optional[int] = Field(
        None, description="0-based TIFF page this form's header was found on"
    )
    invoice_number: str = Field(description="Original invoice, e.g. 14210")
    invoice_adj_number: str = Field(
        description="…C1 id — equals the remittance INVOICE# and is this "
        "record's unique key (SOP §3A Step 9)"
    )
    form_date: Optional[str] = Field(None, description="Form Date (ISO)")
    invoice_date: Optional[str] = Field(
        None,
        description="'...process this invoice dated MM/DD/YYYY' (ISO)",
    )
    division: Optional[str] = None
    lines: List[VendorInvoiceAdjustmentLine] = Field(default_factory=list)
    total_for_adjustment: Optional[float] = Field(
        None, description="Must equal the remittance NET for this …C1 line (Gate D)"
    )

    @field_validator("invoice_adj_number", mode="before")
    @classmethod
    def _normalize_adj_number(cls, v: object) -> str:
        # Same OCR-symbol-stripping as RemittanceLine._normalize_invoice_number.
        return re.sub(r"[^A-Za-z0-9]", "", str(v)).strip().upper()

    @property
    def is_quantity_variance(self) -> bool:
        """Gate C at the form level: every line must be a quantity variance
        (a mixed form with any price/allowance line is not trusted whole)."""
        return bool(self.lines) and all(
            line.is_quantity_variance for line in self.lines
        )

    def lines_total(self) -> float:
        return round(
            sum(line.amount_adjusted or 0.0 for line in self.lines), 2
        )

    def totals_consistent(self, remittance_net: Optional[float] = None) -> bool:
        """Gate D form check: Σ(Amount Adjusted) = Total for Adjustment, and
        (when supplied) that total ties to the remittance NET.

        The printed "Total for Adjustment" line does not reliably OCR at
        all on real documents — confirmed 2026-08-14 against real fixture
        TIFFs: it's dropped outright, not merely garbled — so when it can't
        be read this falls back to Σ(Amount Adjusted) itself as the
        effective total. That is not a guess: SOP Appendix A's worked
        example defines Total for Adjustment as exactly this sum
        ("5 + 22 + 24 cases × $292.50 = $14,917.50"). The fallback only
        applies when EVERY line's Amount Adjusted was itself readable —
        one missing line would silently understate the sum and could mask
        a real shortfall.
        """
        if not self.lines or any(line.amount_adjusted is None for line in self.lines):
            return False
        lines_total = self.lines_total()
        effective_total = (
            self.total_for_adjustment
            if self.total_for_adjustment is not None
            else lines_total
        )
        if abs(lines_total - effective_total) > AMOUNT_TOLERANCE:
            return False
        if remittance_net is not None:
            return abs(abs(effective_total) - abs(remittance_net)) <= AMOUNT_TOLERANCE
        return True

    # Bound on the candidate-combination search below: 3 candidates/line →
    # 3^8 = 6561 combos worst case (observed forms have ≤ 3 lines).
    _MAX_RECONCILE_LINES = 8

    def _line_dollar_candidates(self) -> Optional[List[List[float]]]:
        """Per line, the plausible readings of its dollar amount:
        |Amount Adjusted| as printed, Units In Dispute × unit cost, and
        Units In Dispute × unit cost ÷ 100 (the dropped-decimal-point OCR
        defect — observed live 2026-08-21 on check 8500125089, where unit
        cost read "29250" for a printed "292.50"). None when any line has no
        readable dollar signal at all (nothing to corroborate)."""
        per_line: List[List[float]] = []
        for line in self.lines:
            candidates: List[float] = []
            if line.amount_adjusted is not None:
                candidates.append(round(abs(line.amount_adjusted), 2))
            if line.units_in_dispute is not None and line.unit_cost is not None:
                product = round(line.units_in_dispute * line.unit_cost, 2)
                candidates.extend([product, round(product / 100, 2)])
            deduped = sorted(set(candidates))
            if not deduped:
                return None
            per_line.append(deduped)
        return per_line

    def dollars_corroborated(self, remittance_net: Optional[float]) -> bool:
        """Gate D fallback (SOP §3A Step 8: "If it doesn't tie, re-OCR — the
        numbers are wrong, not the deduction"): EXACTLY ONE combination of
        per-line dollar candidates sums to the remittance NET.

        Engaged only when ``totals_consistent`` fails, i.e. some dollar cell
        misOCR'd. The remittance NET is an INDEPENDENT OCR reading (page 1,
        a different table), so a unique candidate combination tying it
        corroborates the deduction without trusting any one corrupted cell;
        an ambiguous tie (two combinations both summing there) fails rather
        than guessing. The row's synced Invoice Amount is built from the
        Total for Adjustment / remittance NET, and per-line dollar cells are
        never synced, so no repaired value needs persisting — this check
        only decides confirmation.
        """
        if remittance_net is None or not self.lines:
            return False
        if len(self.lines) > self._MAX_RECONCILE_LINES:
            return False
        per_line = self._line_dollar_candidates()
        if per_line is None:
            return False
        target = abs(remittance_net)
        totals = [0.0]
        for candidates in per_line:
            totals = [
                round(total + candidate, 2)
                for total in totals
                for candidate in candidates
            ]
        ties = sum(1 for total in totals if abs(total - target) <= AMOUNT_TOLERANCE)
        return ties == 1


# ---------------------------------------------------------------------------
# Output record (one per confirmed …C1 shortage; SOP §3A Step 9)
# ---------------------------------------------------------------------------

class McLaneShortageRecord(BaseModel):
    """One confirmed …C1 Vendor Invoice Adjustment, flattened for output.

    Same field derivation as upstream's ``McLaneAirtableRow
    .from_confirmed_shortage`` minus the Airtable aliases/workflow flags.
    """

    invoice_adj_number: str = Field(description="Row key, e.g. 14210C1")
    original_invoice_number: Optional[str] = None
    source_filename: Optional[str] = None
    date: Optional[str] = None
    division: Optional[str] = None
    invoice_amount: Optional[float] = Field(
        None, description="Negative — the deducted amount"
    )
    check_number: Optional[str] = None
    check_date: Optional[str] = None
    short_quantity: Optional[float] = Field(
        None, description="Σ Units In Dispute across the form's PO lines"
    )
    reason: Optional[str] = None
    remarks: str = "Shortage"
    distributor: str = DISTRIBUTOR_NAME
    lines: List[VendorInvoiceAdjustmentLine] = Field(default_factory=list)

    @classmethod
    def from_confirmed_shortage(
        cls,
        remittance: RemittanceLine,
        form: VendorInvoiceAdjustmentForm,
        email: Optional[DeductionSupportEmail] = None,
    ) -> "McLaneShortageRecord":
        """Build the record from a shortage line that survived every gate.

        Args:
            remittance: The …C1 remittance line (page 1).
            form: Its matched Vendor Invoice Adjustment Form.
            email: The packet envelope, for check number/date.

        Returns:
            The flattened shortage record.
        """
        quantity = (
            round(sum(line.units_in_dispute or 0.0 for line in form.lines), 2)
            if form.lines
            else None
        )
        invoice_amount = (
            round(-abs(form.total_for_adjustment), 2)
            if form.total_for_adjustment is not None
            else (round(-abs(remittance.net), 2) if remittance.net is not None else None)
        )
        return cls(
            invoice_adj_number=form.invoice_adj_number,
            original_invoice_number=form.invoice_number,
            source_filename=form.source_filename,
            date=form.form_date or form.invoice_date,
            division=remittance.division or form.division,
            invoice_amount=invoice_amount,
            check_number=email.check_number if email else None,
            check_date=email.check_date if email else None,
            short_quantity=quantity,
            reason=next((line.reason for line in form.lines if line.reason), None),
            lines=form.lines,
        )
