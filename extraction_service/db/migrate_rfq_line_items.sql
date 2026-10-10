-- Rebuild line_items to match extracted RFQ PDF tables (med / aut / spec-ref formats).

ALTER TABLE skumaster ADD COLUMN IF NOT EXISTS buyer_part_no TEXT;
CREATE INDEX IF NOT EXISTS idx_skumaster_buyer_part_no
    ON skumaster (buyer_part_no)
    WHERE buyer_part_no IS NOT NULL;

ALTER TABLE rfq_quotes ADD COLUMN IF NOT EXISTS email_subject TEXT;
ALTER TABLE rfq_quotes ADD COLUMN IF NOT EXISTS source_message_date TIMESTAMPTZ;

DROP TABLE IF EXISTS compliance;
DROP TABLE IF EXISTS line_items;

CREATE TABLE line_items (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE,
    line_seq INT NOT NULL,
    line_no INT,
    rfq_format TEXT NOT NULL CHECK (rfq_format IN ('numbered', 'spec_ref', 'description_part')),
    buyer_part_no TEXT,
    description TEXT NOT NULL,
    quantity NUMERIC,
    unit_of_measure TEXT,
    unit_price NUMERIC,
    delivery TEXT,
    is_option BOOLEAN NOT NULL DEFAULT false,
    required_qty_raw TEXT,
    source_attachment TEXT,
    source_page INT,
    source_table TEXT,
    raw_row JSONB NOT NULL DEFAULT '{}'::jsonb,
    skumaster_id BIGINT REFERENCES skumaster(id) ON DELETE SET NULL,
    catalog_id BIGINT REFERENCES catalog(id) ON DELETE SET NULL,
    item_item_id BIGINT REFERENCES item_items(id) ON DELETE SET NULL,
    match_score NUMERIC,
    quoted_unit_price NUMERIC,
    extended_price NUMERIC,
    market_benchmark_price NUMERIC,
    price_rationale TEXT,
    line_status TEXT NOT NULL DEFAULT 'parsed'
        CHECK (line_status IN (
            'pending', 'parsed', 'matched', 'priced',
            'ambiguous', 'unmatched', 'rejected'
        )),
    clarifying_question TEXT,
    availability_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (quote_id, line_seq)
);

CREATE INDEX IF NOT EXISTS idx_line_items_quote ON line_items (quote_id);
CREATE INDEX IF NOT EXISTS idx_line_items_part_no ON line_items (buyer_part_no);

CREATE TABLE compliance (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE,
    line_item_id BIGINT REFERENCES line_items(id) ON DELETE CASCADE,
    requirement_code TEXT NOT NULL,
    requirement_label TEXT NOT NULL,
    matched_pattern TEXT,
    source_text TEXT,
    status TEXT NOT NULL DEFAULT 'required'
        CHECK (status IN (
            'required', 'document_missing', 'satisfied', 'waived', 'send_to_buyer'
        )),
    required_document_type TEXT,
    attachment_path TEXT,
    buyer_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_compliance_quote ON compliance (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_line_item ON compliance (line_item_id);
