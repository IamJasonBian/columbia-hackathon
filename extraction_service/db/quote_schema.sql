-- TRACK 2 | RudCo | RFQ → quote
-- Apply: psql "$DATABASE_URL" -f extraction_service/db/quote_schema.sql

CREATE TABLE IF NOT EXISTS skumaster (
    id BIGSERIAL PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    buyer_part_no TEXT,
    name TEXT NOT NULL,
    description TEXT,
    material TEXT,
    grade TEXT,
    spec_standard TEXT,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    unit_of_measure TEXT,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skumaster_material_grade
    ON skumaster (material, grade)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_skumaster_buyer_part_no
    ON skumaster (buyer_part_no)
    WHERE buyer_part_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS catalog (
    id BIGSERIAL PRIMARY KEY,
    skumaster_id BIGINT REFERENCES skumaster(id) ON DELETE SET NULL,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    material TEXT,
    grade TEXT,
    spec_standard TEXT,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    unit_of_measure TEXT,
    list_price NUMERIC,
    currency TEXT NOT NULL DEFAULT 'USD',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS item_items (
    id BIGSERIAL PRIMARY KEY,
    catalog_id BIGINT NOT NULL REFERENCES catalog(id) ON DELETE CASCADE,
    supplier_sku TEXT,
    description TEXT NOT NULL,
    market_benchmark_price NUMERIC,
    last_price_updated_at TIMESTAMPTZ,
    in_stock BOOLEAN,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (catalog_id, supplier_sku)
);

CREATE TABLE IF NOT EXISTS rfq_quotes (
    id BIGSERIAL PRIMARY KEY,
    mailbox TEXT,
    gmail_message_id TEXT,
    customer_id TEXT,
    rfq_number TEXT,
    email_subject TEXT,
    source_message_date TIMESTAMPTZ,
    buyer_organization TEXT,
    workflow_status TEXT NOT NULL DEFAULT 'intake'
        CHECK (workflow_status IN (
            'intake', 'parsing', 'needs_clarification',
            'human_review', 'rejected', 'ready_to_send'
        )),
    reject_reason TEXT,
    has_attachments BOOLEAN NOT NULL DEFAULT false,
    parsed JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (mailbox, gmail_message_id)
);

CREATE TABLE IF NOT EXISTS line_items (
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

CREATE INDEX IF NOT EXISTS idx_line_items_part_no ON line_items (buyer_part_no);

CREATE TABLE IF NOT EXISTS compliance (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE,
    line_item_id BIGINT REFERENCES line_items(id) ON DELETE CASCADE,
    requirement_code TEXT NOT NULL,
    requirement_label TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'email_text',
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

CREATE INDEX IF NOT EXISTS idx_line_items_quote ON line_items (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_quote ON compliance (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_line_item ON compliance (line_item_id);

ALTER TABLE rfq_quotes ADD COLUMN IF NOT EXISTS customer_id TEXT;
ALTER TABLE compliance ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'email_text';
