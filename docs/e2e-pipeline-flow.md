# RFQ pipeline — end-to-end flow

This document describes how email RFQs move through the Columbia hackathon stack today:
Gmail poll → extract → PostgreSQL mailbox store → SQLite task queue → pipeline worker →
rfq_quotes + doc_lines.lines + compliance → optional Nicrron classification → review UI.

For interactive Mermaid diagrams, see [email-extraction-pipeline.html](./email-extraction-pipeline.html).
The root [README](../README.md) also has an architecture overview and sequence diagram.

**Persistence:** PostgreSQL via `psycopg` (`PostgresSink`, `QuoteRepository`, review service pool). Code lives in `services/extraction` and `services/review`.
There is no Prisma ORM in this repository; a separate Prisma app would read/write the same tables.

## Prerequisites

- OAuth for the mailbox (`EMAIL_OAUTH_STORE` → file or Firestore).
- `DATABASE_URL` (or `POSTGRES_URL`).
- Optional: `NICRRON_API_KEY` for classification and customer compliance enrichment.
- Processes: `bootstrap/poll_mailbox.py`, `bootstrap/worker.py`, optional `services.review` (:8014).

## 1. Poll tick (Gmail → extract → raw store)

**Code:** `MailboxPoller` in `bootstrap/poll_mailbox.py` with `PostgresMailboxStore` (default).

1. Load poll state from Postgres (`mailbox_poll_state`, seen Gmail IDs).
2. Gmail `messages.list` with the document query and an `after:` watermark.
3. For each new message ID (oldest first):
   - Fetch raw RFC822.
   - `extract_document` → `ExtractedDocument` (body, attachments, PDF/XLS tables, OCR if enabled).
   - `PostgresSink.write` → upsert `mailbox_messages` (full `document` JSONB).
   - Advance seen IDs and save poll state.
4. Stdout: `NEW {gmail_id} | date | from | subject | attachments…`

## 2. Enqueue (poller → SQLite, no quote writes)

**Code:** `RfqTaskEnqueuer` (`on_extracted` after a successful save).

1. Enqueue job type `rfq_line_items` in `app_storage/rfq/service_queue.db` (`ServiceQueue`).
2. Idempotency key: `line_items:{mailbox}:{gmail_message_id}`.
3. Payload includes `task_id` like `rfq-{uuid12}`.
4. Stdout: `TASK rfq-… queued for {gmail_id}`.

The poller does **not** write `rfq_quotes` or `doc_lines.lines`; the worker does, so deploys can hot-reload extraction logic without changing the poller.

## 3. Worker job: `rfq_line_items`

**Code:** `PipelineWorker` in `bootstrap/worker.py` → `persist_extracted_document`.

1. Claim the next job (`rfq_line_items` or `nicrron_classify`).
2. SQLite stages: `intake` → `parse_lines` → `persist` → complete.
3. `load_extracted_document`:
   - Prefer `app_storage/pulls/{mailbox}/{gmail_id}.json` if present (hot reload after re-extract).
   - Else read `mailbox_messages.document` from Postgres.
4. `persist_extracted_document`:
   - `extract_lines_from_document` → normalized rows (`line_seq`, `line_no`, `buyer_part_no`, qty/uom, `raw_row`, etc.).
   - `scan_compliance_text` (+ buyer cert line) → initial `compliance` rows (`source=email_text`).
   - `QuoteRepository.save_quote_bundle` → upsert `rfq_quotes`, replace `doc_lines.lines`, replace all compliance for the quote (`source=email_text`, `line_id` NULL).
5. Set job `postgres_quote_id`, mark job complete.
6. If `ENABLE_CLASSIFICATION_PIPELINE` (default on): enqueue `nicrron_classify` (idempotent `classify:{mailbox}:{gmail_id}`).

## 4. Worker job: `nicrron_classify`

**Code:** `ClassificationWorker` in the same pipeline worker.

1. Hot-load the document again.
2. `load_customer_compliance()` → map sender → `customer_id` and required document codes (`config/customer_compliance.json`).
3. `assess_email_compliance` (Nicrron chat API) → email label, confidence, per-requirement attachment match.
4. `QuoteRepository.update_classification` → merge into `rfq_quotes.parsed` (classification blob; `human_review` if sender unmapped).
5. For mapped customers: build `compliance` rows per line × requirement (`line_id` set) (`source=customer_mapping`), via `replace_compliance_source` (does not wipe regex `email_text` rows).
6. Complete job; classification is also stored on the SQLite job payload.

Model calls may emit traces to [PRISM / Block Convey](https://blockconvey.com/docs) when configured (see README).

## 5. Operations and visibility

| Surface | Role |
|--------|------|
| SQLite queue | Job status, stage, `postgres_quote_id`, errors. |
| `bootstrap/ingest_pulls_to_quotes.py` | Backfill: enqueue-only or `--process-now` for pulls on disk. |
| `bootstrap/init_postgres.py` | Schema bootstrap + idempotent `line_items` → `doc_lines.lines` migration. |

## 6. Human review

**Code:** `services/review` :8014 and `frontend/`.

1. Lists quotes, lines, and compliance from `rfq_quotes`, `doc_lines.lines`, and `compliance`.
2. `doc_lines.line_locks` lease per row (heartbeat TTL).
3. `PATCH /api/lines/{id}` under lease; `PATCH /api/compliance/{id}` for status/buyer message (no lease); Google sign-in or `REVIEW_DEV_AUTH` for local testing.

## Happy-path timeline (one message)

```text
Gmail new RFQ email
  → poller: extract_document
  → Postgres: mailbox_messages
  → SQLite: rfq_line_items (task_id rfq-…)
  → worker: load document → doc_lines.lines + rfq_quotes + email_text compliance
  → SQLite: nicrron_classify (task_id cls-…)
  → worker: Nicrron → rfq_quotes.parsed.classification + customer_mapping compliance
  → reviewer: UI edits locked doc_lines.lines + compliance in Postgres
```

## Diagram vs implementation

- [email-extraction-pipeline.html](./email-extraction-pipeline.html) shows poller and worker writing to the database in one sequence; **current code** splits **mailbox ingest (poller)** from **quote ingest (worker + SQLite queue)**.
- `_state.json` in the HTML diagram is legacy `--file-store`; the default path uses `mailbox_poll_state` in Postgres.
