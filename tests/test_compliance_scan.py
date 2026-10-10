from __future__ import annotations

from extraction_service.quote.compliance_scan import scan_compliance_text


def test_detects_mtr_and_coo():
    text = "Vendor must provide mill test report and country of origin certificate."
    hits = scan_compliance_text(text)
    codes = {h.requirement_code for h in hits}
    assert "mtr" in codes
    assert "coo" in codes
