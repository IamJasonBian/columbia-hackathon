# Columbia hackathon

RFQ email pipeline (extraction → document lines → compliance) plus a review UI,
organized as three services under `services/`:

| Package | Role |
| --- | --- |
| `services/extraction` | Gmail poller, document extractors, SQLite task queue, pipeline worker, Nicrron classification |
| `services/review` | Review API (:8014) over `doc_lines.lines` and `compliance`, serves `frontend/` |
| `services/validation` | Customer config validation API (:8010), in-memory jobs |

## Project links

- GitHub: [IamJasonBian/columbia-hackathon](https://github.com/IamJasonBian/columbia-hackathon)
- Hackathon working document: [Quote Document (Sent)](https://docs.google.com/document/d/1TeD60bNlQqb1bOKdDRpUHgEC1B-TAxk094qYutOpqyM/edit?tab=t.0)
- **Docs:** [docs/](./docs/) — [E2E pipeline flow](./docs/e2e-pipeline-flow.md), [Mermaid diagrams](./docs/email-extraction-pipeline.html)

## System architecture

```mermaid
flowchart LR
    Gmail[Gmail API] --> Poller[Mailbox poller]
    Poller --> Parse[Document extractors<br/>email · PDF · OCR · spreadsheet]

    Parse --> Mail[(PostgreSQL<br/>mailbox documents)]
    Poller --> Queue[(SQLite task queue)]
    Queue --> Worker[Pipeline worker]
    Mail --> Worker
    Worker --> Quotes[(PostgreSQL<br/>rfq_quotes · doc_lines.lines · compliance)]

    Worker --> Nicrron[Nicrron API]
    Nicrron -. model traces .-> Prism[PRISM / Block Convey]

    Quotes --> Review[Review API :8014]
    Review --> UI[React review UI]
    Validation[Validation API :8010] --> Memory[(In-memory jobs)]
```

### RFQ ingestion and review flow

```mermaid
sequenceDiagram
    participant G as Gmail
    participant P as Mailbox poller
    participant DB as PostgreSQL
    participant Q as SQLite queue
    participant W as Pipeline worker
    participant N as Nicrron
    participant O as PRISM
    participant A as Review API
    participant U as Review UI

    P->>G: Fetch new RFC 822 messages
    P->>P: Extract body, attachments, tables, and OCR
    P->>DB: Store parsed mailbox document
    P->>Q: Enqueue rfq_line_items
    W->>Q: Claim task
    W->>DB: Upsert quote, doc lines, and rule-based compliance
    W->>Q: Enqueue nicrron_classify
    W->>N: Classify email and assess required documents
    N-->>W: Classification and compliance JSON
    W-->>O: Send model trace asynchronously
    W->>DB: Store classification and compliance results
    U->>A: Load quotes and acquire row locks
    A->>DB: Read quotes and persist leases
    U->>A: Save pricing and compliance decisions
    A->>DB: Recheck lease and commit edits
```

See [docs/e2e-pipeline-flow.md](./docs/e2e-pipeline-flow.md) for a step-by-step narrative (poll → queue → worker → Postgres → review).

## Quick start

```bash
cd "/Users/jasonzb/columbia hackathon"
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export PYTHONPATH="$PWD" SKIP_AUTH=1
./bootstrap/run_services.sh      # validation :8010 + review :8014
pytest
```

Validation routes take `Authorization: Bearer $LOCAL_SA_TOKEN` unless `SKIP_AUTH=1`.

## Validation API (`services/validation`, port 8010)

Every customer config entry needs `customer` and `email` (validated format),
plus `baseId`, `tableIdOrName`, `bucketname`, `project_id`.

- `POST /validation-jobs`: store `{ kind, payload }` (`global_config` \| `new_customer` \| `customer_personal`)
- `POST /validation-jobs/{id}/run`: run the rules and persist `last_result`. `new_customer` takes `{ "existing_global": {...} }`
- `POST /validate/global`: validate a full `{ "data": [customer, ...] }` config directly

## Review UI (`services/review` + `frontend/`, port 8014)

Google sign-in (anyone can sign up) and a table editor for `doc_lines.lines`.
Clicking a row takes a lease on it; other users see it locked and can't
write it until the holder saves, cancels, or goes idle.

- Locks are rows in `doc_lines.line_locks` (one per line, TTL `LOCK_TTL_SECONDS`, default 45s)
  renewed by a client heartbeat; a lapsed lease is free for anyone. A user holds at most one lock.
- `PATCH /api/lines/{id}` re-checks the lease inside a transaction (`FOR UPDATE`): 409 if you don't hold it.
- `app_users`, `app_sessions`, and `doc_lines.line_locks` are created on startup (`services/review/schema.sql`).
  Run `bootstrap/init_postgres.py` first so `doc_lines.lines` exists.

```bash
# Google: create an OAuth 2.0 "Web application" client, add http://localhost:5173
# and http://localhost:8014 as Authorized JavaScript origins, then:
export GOOGLE_CLIENT_ID=....apps.googleusercontent.com
PYTHONPATH="$PWD" python -m services.review.main      # API (+ serves frontend/dist if built)
cd frontend && npm install && npm run dev            # http://localhost:5173, proxies /api
# or: npm run build, then open http://localhost:8014
```

`REVIEW_DEV_AUTH=1` adds an email-only sign-in for testing locks with two users locally.

API: `GET /api/quotes?q=`, `GET /api/quotes/{id}` (lines + live locks + compliance),
`POST|DELETE /api/lines/{id}/lock`, `PATCH /api/lines/{id}?release=true`,
`PATCH /api/compliance/{id}`.

## Schema

`bootstrap/init_postgres.py` (or any `PostgresSink.ensure_schema()`) creates the pipeline tables;
the review service adds its own on startup.

| Table | Written by | Purpose |
| --- | --- | --- |
| `mailbox_messages` | Poller | Full extracted document (JSONB) per Gmail message |
| `mailbox_poll_state` | Poller | `after:` watermark per mailbox |
| `rfq_quotes` | Worker | One quote per message; `customer_id`, `workflow_status`, `parsed.classification` |
| `doc_lines.lines` | Worker; reviewers edit | Parsed document lines (qty, UoM, part no, pricing, `line_status`) |
| `compliance` | Worker; reviewers edit | Required documents/certs per quote or per line (see below) |
| `doc_lines.line_locks` | Review API | Per-line edit leases |
| `app_users`, `app_sessions` | Review API | Sign-in |

Upgrading an existing database is automatic and idempotent: `public.line_items` moves to
`doc_lines.lines` with rows and ids intact, `compliance.line_item_id` becomes `line_id`, and the
old `line_item_locks` table (short-lived leases only) is dropped. The previously unused tables
(`skumaster`, `catalog`, `item_items`, `mailbox_table_rows`) and the line columns
`skumaster_id`, `catalog_id`, `item_item_id` are no longer created. Existing copies are left
in place; drop them by hand once you're sure nothing outside this repo reads them:

```sql
ALTER TABLE doc_lines.lines DROP COLUMN IF EXISTS skumaster_id,
  DROP COLUMN IF EXISTS catalog_id, DROP COLUMN IF EXISTS item_item_id;
DROP TABLE IF EXISTS item_items, catalog, skumaster, mailbox_table_rows;
```

## Compliance documents

A `compliance` row is one required document or certification. It always belongs to a quote
(`quote_id → rfq_quotes.id`). Its scope comes from `line_id`:

- `line_id IS NULL`: quote-level. Applies to the whole RFQ (UI shows "Whole quote").
- `line_id → doc_lines.lines.id`: line-level. Applies to that one line (UI shows "Line N").

Both foreign keys `ON DELETE CASCADE`: deleting a quote or a line deletes its compliance rows.

Key columns: `requirement_code`, `requirement_label`, `source`, `status`
(`required` · `document_missing` · `satisfied` · `waived` · `send_to_buyer`),
`attachment_path` (evidence), `buyer_message` (what to ask the buyer for), `source_text`.

| Operation | How | Scope / rows touched |
| --- | --- | --- |
| **Create (rules)** | Worker job `rfq_line_items` → `QuoteRepository.save_quote_bundle` → `replace_compliance(quote_id, records)` | Deletes **all** compliance rows for the quote, then inserts regex hits from `scan_compliance_text` plus any buyer-cert line. `source='email_text'`, `line_id` NULL |
| **Create (customer)** | Worker job `nicrron_classify` → `replace_compliance_source(quote_id, 'customer_mapping', records)` | Deletes only `source='customer_mapping'` rows, then inserts one row per **line × required document** from `config/customer_compliance.json`, with Nicrron's `satisfied`/`document_missing` verdict and `attachment_path`. Unmapped sender: inserts nothing |
| **Read** | `GET /api/quotes/{id}` → `compliance[]` (ordered by id) | All rows for the quote; join to `lines[]` on `line_id` |
| **Read (summary)** | `GET /api/quotes` → `open_compliance_count` | Count of rows in `required` or `document_missing` |
| **Update** | `PATCH /api/compliance/{id}` with `{ "status"?, "buyer_message"? }` | One row. No line lease is needed and the last write wins. 404 if the row is gone, 422 on `status: null`. Other fields reject (`extra="forbid"`) |
| **Delete** | No endpoint | Rows go away when the worker re-runs (replace) or via cascade from the quote/line |

Re-parsing a message replaces its lines (new ids) and all of its compliance rows. Reviewer
edits to `status`/`buyer_message` don't survive a re-parse.

```bash
# mark a requirement satisfied
curl -X PATCH http://127.0.0.1:8014/api/compliance/42 \
  -H 'Content-Type: application/json' -b review_session=... \
  -d '{"status": "satisfied"}'
```

## Gmail persistence

Mailbox polling uses PostgreSQL as its source of truth:

```bash
python bootstrap/init_postgres.py
EMAIL_OAUTH_STORE=firestore python bootstrap/poll_mailbox.py mailbox@example.com
```

Both commands require `DATABASE_URL`. Use `--file-store` only for offline
development.

## Task queue and worker

Mailbox polling writes durable jobs to the local SQLite queue at
`app_storage/rfq/service_queue.db`; it does not perform model or quote work in
the polling process. Jobs use idempotency keys so polling the same Gmail
message does not create duplicate work.

| Job type | Producer | Worker action |
| --- | --- | --- |
| `rfq_line_items` | Mailbox poller | Parse tables and persist the quote, `doc_lines.lines`, and rule-based compliance to PostgreSQL |
| `nicrron_classify` | Pipeline worker | Ask Nicrron to classify the email and assess customer compliance documents |

```mermaid
stateDiagram-v2
    [*] --> pending: enqueue
    pending --> running: worker claims
    running --> succeeded: complete
    running --> pending: retry with backoff
    running --> dead: retry limit reached
    succeeded --> [*]
    dead --> [*]
```

Run the worker as a separate process:

```bash
export DATABASE_URL=postgresql://user:pass@host:5432/mydb
python bootstrap/worker.py
```

The SQLite queue is intended for a single shared host; use a distributed queue
before running workers on multiple machines.

## Nicrron classification setup

![Nicrron classification and pipeline logs](./docs/nicrron-calls-and-logs.gif)

Nicrron provides the OpenAI-compatible endpoint used by queued RFQ
classification and compliance jobs.

1. Sign in to [Nicrron](https://www.nicrron.ai/) and open **Keys**.
2. Generate a Nicrron API key (`sk-af-...`).
3. Click **Add Provider Key**, choose the model provider, and add that
   provider's key. Nicrron uses bring-your-own-key (BYOK).
4. Add the backend-only configuration to `.env` or your secret manager:

```bash
NICRRON_API_KEY=sk-af-...
NICRRON_MODEL=nicrron/fastest
NICRRON_BASE_URL=https://api.nicrron.ai/v1
ENABLE_CLASSIFICATION_PIPELINE=1
```

5. Export or source those variables, then start the worker:

```bash
set -a; source .env; set +a
python bootstrap/worker.py
```

Keep the Nicrron key out of browser code and source control.

## PRISM observability

![PRISM async trace delivery from Nicrron calls](./docs/prism-trace-flow.gif)

![PRISM setup doctor, agents, and verification](./docs/prism-setup-handshake.gif)

Nicrron compliance calls emit traces to
[PRISM](https://blockconvey.com/docs) when both `PRISMTRACE_API_KEY` and
`PRISMTRACE_PROJECT_ID` are set. Delivery runs off the request path and fails
open, so an observability outage does not break the pipeline.

1. Create an account at [prism.blockconvey.com](https://prism.blockconvey.com/signup).
2. In **Settings → Project**, copy the project ID. Use a separate project for
   each environment.
3. On the API keys page, create an **ingest-only** key and copy it when shown.
4. Add the values to `.env` or your deployment secret manager:

```bash
PRISMTRACE_HOST=https://prism.blockconvey.com
PRISMTRACE_PROJECT_ID=00000000-0000-0000-0000-000000000000
PRISMTRACE_API_KEY=pt-sk-...
```

5. Verify the credentials before starting traffic:

```bash
set -a; source .env; set +a
curl -sS -X POST "$PRISMTRACE_HOST/api/setup-doctor/handshake" \
  -H "Content-Type: application/json" \
  -H "X-PRISMtrace-Key: $PRISMTRACE_API_KEY" \
  -d '{"project_id":"'"$PRISMTRACE_PROJECT_ID"'","send_test_trace":true}'
```

6. Start the pipeline worker. Calls appear under the agent `nicrron-compliance`.

Trace payloads include model prompts and outputs, which can contain document
content and customer data. Use an environment-specific PRISM project and never
place the PRISM key in frontend code.

## Customer compliance mapping

Queued email classification also loads `config/customer_compliance.json`. An exact sender
email wins over a sender-domain match. Each customer defines the compliance documents that
must apply to every extracted document line:

```json
{
  "customer_id": "mock-customer",
  "sender_domains": ["mockcustomer.local"],
  "compliance_documents": [
    {
      "code": "food_safety_plan_haccp",
      "document_name": "Mock Food Safety Plan HACCP.pdf",
      "prompt": "Determine whether an attachment is a HACCP food safety plan."
    }
  ]
}
```

The worker sends attachment filenames and locally parsed PDF/text contents to Nicrron.
Matched documents are stored as `satisfied`; absent documents are stored as
`document_missing`. The same result is linked to each line in `doc_lines.lines`
(see [Compliance documents](#compliance-documents)). Set
`CUSTOMER_COMPLIANCE_PATH` to use another local mapping file. Unmapped senders retain a
null `customer_id` and are routed to human review.

