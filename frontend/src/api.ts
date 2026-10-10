export type User = { id: number; email: string; name: string | null; picture: string | null };

export type Config = {
  google_client_id: string | null;
  dev_auth: boolean;
  lock_ttl_seconds: number;
};

export type LockInfo = {
  user_id: number;
  email: string;
  name: string | null;
  picture?: string | null;
  acquired_at?: string;
  expires_at: string;
};

export type QuoteSummary = {
  id: number;
  rfq_number: string | null;
  email_subject: string | null;
  buyer_organization: string | null;
  workflow_status: string;
  source_message_date: string | null;
  line_count: number;
  locked_count: number;
  open_compliance_count: number;
};

export const COMPLIANCE_STATUSES = ["required", "document_missing", "satisfied", "waived", "send_to_buyer"] as const;

export type Compliance = {
  id: number;
  quote_id: number;
  line_id: number | null;
  requirement_code: string;
  requirement_label: string;
  source: string;
  matched_pattern: string | null;
  source_text: string | null;
  status: (typeof COMPLIANCE_STATUSES)[number];
  required_document_type: string | null;
  attachment_path: string | null;
  buyer_message: string | null;
};

// NUMERIC columns arrive as strings (Decimal) — keep them that way to avoid float drift.
type Num = string | number | null;

export type Line = {
  id: number;
  quote_id: number;
  line_seq: number;
  line_no: number | null;
  rfq_format: string;
  buyer_part_no: string | null;
  description: string;
  quantity: Num;
  unit_of_measure: string | null;
  unit_price: Num;
  delivery: string | null;
  is_option: boolean;
  required_qty_raw: string | null;
  source_attachment: string | null;
  source_page: number | null;
  source_table: string | null;
  match_score: Num;
  quoted_unit_price: Num;
  extended_price: Num;
  market_benchmark_price: Num;
  price_rationale: string | null;
  line_status: string;
  clarifying_question: string | null;
  availability_note: string | null;
  lock: LockInfo | null;
};

export type QuoteDetail = Omit<QuoteSummary, "line_count" | "locked_count" | "open_compliance_count"> & {
  mailbox: string | null;
  lines: Line[];
  compliance: Compliance[];
};

export type LockGrant = {
  line_id: number;
  user_id: number;
  expires_at: string;
  ttl_seconds: number;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }

  /** The current holder when a lock/write was refused with 409. */
  get lock(): LockInfo | null {
    return (this.detail as { lock?: LockInfo })?.lock ?? null;
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new ApiError(res.status, data?.detail ?? res.statusText);
  return data as T;
}

export const api = {
  config: () => call<Config>("GET", "/config"),
  me: () => call<User>("GET", "/me"),
  loginGoogle: (credential: string) => call<User>("POST", "/auth/google", { credential }),
  loginDev: (email: string) => call<User>("POST", "/auth/dev", { email }),
  logout: () => call<{ ok: boolean }>("POST", "/auth/logout"),
  quotes: (q: string) => call<QuoteSummary[]>("GET", `/quotes?q=${encodeURIComponent(q)}`),
  quote: (id: number) => call<QuoteDetail>("GET", `/quotes/${id}`),
  lock: (lineId: number) => call<LockGrant>("POST", `/lines/${lineId}/lock`),
  unlock: (lineId: number) => call<{ released: boolean }>("DELETE", `/lines/${lineId}/lock`),
  save: (lineId: number, patch: Partial<Line>, release: boolean) =>
    call<Line>("PATCH", `/lines/${lineId}?release=${release}`, patch),
  saveCompliance: (id: number, patch: Partial<Pick<Compliance, "status" | "buyer_message">>) =>
    call<Compliance>("PATCH", `/compliance/${id}`, patch),
  /** Fire-and-forget release that survives tab close. */
  unlockBeacon: (lineId: number) => navigator.sendBeacon(`/api/lines/${lineId}/unlock`),
};
