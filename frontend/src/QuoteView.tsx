import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type LineItem, type LockInfo, type QuoteDetail, type User } from "./api";
import { CompliancePanel } from "./CompliancePanel";
import { LockIcon } from "./icons";

const POLL_MS = 3000;

type FieldKind = "text" | "textarea" | "number" | "status" | "bool";
const FIELDS: { key: keyof LineItem; label: string; kind: FieldKind }[] = [
  { key: "buyer_part_no", label: "Buyer part #", kind: "text" },
  { key: "description", label: "Description", kind: "textarea" },
  { key: "quantity", label: "Quantity", kind: "number" },
  { key: "unit_of_measure", label: "UoM", kind: "text" },
  { key: "unit_price", label: "Unit price (RFQ)", kind: "number" },
  { key: "quoted_unit_price", label: "Quoted unit price", kind: "number" },
  { key: "extended_price", label: "Extended price", kind: "number" },
  { key: "market_benchmark_price", label: "Market benchmark", kind: "number" },
  { key: "delivery", label: "Delivery", kind: "text" },
  { key: "line_status", label: "Status", kind: "status" },
  { key: "is_option", label: "Optional line", kind: "bool" },
  { key: "price_rationale", label: "Price rationale", kind: "textarea" },
  { key: "clarifying_question", label: "Clarifying question", kind: "textarea" },
  { key: "availability_note", label: "Availability note", kind: "textarea" },
];
const STATUSES = ["pending", "parsed", "matched", "priced", "ambiguous", "unmatched", "rejected"];

type Draft = Record<string, string | boolean>;

function toDraft(line: LineItem): Draft {
  const d: Draft = {};
  for (const f of FIELDS) {
    const v = line[f.key];
    d[f.key] = f.kind === "bool" ? Boolean(v) : v == null ? "" : String(v);
  }
  return d;
}

/** Only the fields the user actually changed; blank optional fields become null. */
function diff(line: LineItem, draft: Draft): Record<string, unknown> {
  const before = toDraft(line);
  const out: Record<string, unknown> = {};
  for (const f of FIELDS) {
    const v = draft[f.key];
    if (v === before[f.key]) continue;
    out[f.key] = typeof v === "boolean" ? v : v.trim() === "" ? null : f.kind === "number" ? v.trim() : v;
  }
  return out;
}

const fmt = (v: unknown) => (v == null || v === "" ? "—" : String(v));
const who = (l: LockInfo) => l.name ?? l.email;

export function QuoteView({ quoteId, me, ttlSeconds }: { quoteId: number; me: User; ttlSeconds: number }) {
  const [quote, setQuote] = useState<QuoteDetail | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const activeRef = useRef<number | null>(null);
  activeRef.current = activeId;

  const refresh = useCallback(() => api.quote(quoteId).then(setQuote), [quoteId]);

  const flash = (kind: "info" | "error", text: string) => {
    setToast({ kind, text });
    setTimeout(() => setToast((t) => (t?.text === text ? null : t)), 4000);
  };

  // Poll so other users' locks and saved edits show up.
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  // Heartbeat keeps the lease alive while the editor is open.
  useEffect(() => {
    if (activeId == null) return;
    const t = setInterval(() => {
      api.lock(activeId).catch((e) => {
        setActiveId(null);
        flash("error", e instanceof ApiError && e.lock ? `Lost lock — ${who(e.lock)} took this row` : `Lost lock: ${e.message}`);
        refresh();
      });
    }, Math.max(1000, (ttlSeconds * 1000) / 3));
    return () => clearInterval(t);
  }, [activeId, ttlSeconds, refresh]);

  // Release on tab close / quote switch.
  useEffect(() => {
    const onUnload = () => activeRef.current != null && api.unlockBeacon(activeRef.current);
    window.addEventListener("pagehide", onUnload);
    return () => {
      window.removeEventListener("pagehide", onUnload);
      if (activeRef.current != null) api.unlock(activeRef.current).catch(() => {});
    };
  }, []);

  const open = async (line: LineItem) => {
    if (line.id === activeId || busy) return;
    if (line.lock && line.lock.user_id !== me.id) {
      flash("info", `${who(line.lock)} is editing this row`);
      return;
    }
    setBusy(true);
    try {
      // Server drops any other lease we hold, so no explicit unlock of the previous row.
      await api.lock(line.id);
      setActiveId(line.id);
      setDraft(toDraft(line));
      refresh();
    } catch (e) {
      flash("error", e instanceof ApiError && e.lock ? `${who(e.lock)} just locked this row` : String((e as Error).message));
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const close = async () => {
    const id = activeId;
    setActiveId(null);
    if (id != null) await api.unlock(id).catch(() => {});
    refresh();
  };

  const save = async (line: LineItem) => {
    const patch = diff(line, draft);
    setBusy(true);
    try {
      await api.save(line.id, patch, true);
      setActiveId(null);
      flash("info", Object.keys(patch).length ? `Saved line ${line.line_seq}` : "No changes");
      refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setActiveId(null);
        flash("error", e.lock ? `Not saved — ${who(e.lock)} holds the lock` : "Not saved — your lock expired");
        refresh();
      } else {
        flash("error", (e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  if (!quote) return <div className="center muted">Loading quote…</div>;

  return (
    <div className="quote-view">
      <div className="quote-head">
        <div>
          <div className="muted">{quote.email_subject}</div>
          <h2>{quote.rfq_number ?? `Quote ${quote.id}`}</h2>
        </div>
        <span className={`pill status-${quote.workflow_status}`}>{quote.workflow_status}</span>
      </div>
      <CompliancePanel rows={quote.compliance} lines={quote.line_items} onSaved={refresh} onError={(m) => flash("error", m)} />

      <section className="panel" aria-labelledby="lines-h">
      <div className="panel-head">
        <h3 id="lines-h">Line items</h3>
        <span className="muted">Click a row to lock it · releases after {ttlSeconds}s idle</span>
      </div>
      <div className="table-scroll">
        <table className="lines">
          <thead>
            <tr>
              <th>#</th>
              <th>Part #</th>
              <th>Description</th>
              <th className="num">Qty</th>
              <th>UoM</th>
              <th className="num">Unit</th>
              <th className="num">Quoted</th>
              <th className="num">Ext.</th>
              <th>Status</th>
              <th>Lock</th>
            </tr>
          </thead>
          <tbody>
            {quote.line_items.map((line) => {
              const mine = line.id === activeId;
              const lockedByOther = !!line.lock && line.lock.user_id !== me.id;
              return (
                <LineRows
                  key={line.id}
                  line={line}
                  mine={mine}
                  lockedByOther={lockedByOther}
                  draft={draft}
                  setDraft={setDraft}
                  busy={busy}
                  onOpen={() => open(line)}
                  onSave={() => save(line)}
                  onCancel={close}
                />
              );
            })}
          </tbody>
        </table>
      </div>
      </section>
      {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
    </div>
  );
}

function LineRows(props: {
  line: LineItem;
  mine: boolean;
  lockedByOther: boolean;
  draft: Draft;
  setDraft: (d: Draft) => void;
  busy: boolean;
  onOpen: () => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { line, mine, lockedByOther, draft, setDraft, busy } = props;
  const cls = mine ? "row mine" : lockedByOther ? "row locked" : "row";
  return (
    <>
      <tr className={cls} onClick={props.onOpen} title={lockedByOther ? `Locked by ${who(line.lock!)}` : "Click to edit"}>
        <td>{line.line_seq}</td>
        <td className="mono">{fmt(line.buyer_part_no)}</td>
        <td className="desc">{line.description}</td>
        <td className="num">{fmt(line.quantity)}</td>
        <td>{fmt(line.unit_of_measure)}</td>
        <td className="num">{fmt(line.unit_price)}</td>
        <td className="num">{fmt(line.quoted_unit_price)}</td>
        <td className="num">{fmt(line.extended_price)}</td>
        <td>
          <span className={`pill line-${line.line_status}`}>{line.line_status}</span>
        </td>
        <td>{mine ? <span className="pill mine">Editing · you</span> : line.lock ? <span className="pill locked">
              <LockIcon /> {who(line.lock)}
            </span> : null}</td>
      </tr>
      {mine && (
        <tr className="editor-row">
          <td colSpan={10}>
            <form
              className="editor"
              onSubmit={(e) => {
                e.preventDefault();
                props.onSave();
              }}
              onKeyDown={(e) => e.key === "Escape" && props.onCancel()}
            >
              <div className="grid">
                {FIELDS.map((f) => (
                  <label key={f.key} className={f.kind === "textarea" ? "field wide" : "field"}>
                    <span>{f.label}</span>
                    {f.kind === "textarea" ? (
                      <textarea rows={2} value={draft[f.key] as string} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })} />
                    ) : f.kind === "status" ? (
                      <select value={draft[f.key] as string} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
                        {STATUSES.map((s) => (
                          <option key={s}>{s}</option>
                        ))}
                      </select>
                    ) : f.kind === "bool" ? (
                      <input type="checkbox" checked={draft[f.key] as boolean} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.checked })} />
                    ) : (
                      <input
                        inputMode={f.kind === "number" ? "decimal" : undefined}
                        pattern={f.kind === "number" ? "-?\\d*(\\.\\d+)?" : undefined}
                        value={draft[f.key] as string}
                        onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                      />
                    )}
                  </label>
                ))}
              </div>
              <div className="source muted">
                Source: {fmt(line.source_attachment)} · page {fmt(line.source_page)} · raw qty “{fmt(line.required_qty_raw)}” · format {line.rfq_format}
              </div>
              <div className="actions">
                <button type="button" className="ghost" onClick={props.onCancel} disabled={busy}>
                  Cancel (Esc)
                </button>
                <button type="submit" disabled={busy}>
                  Save &amp; unlock
                </button>
              </div>
            </form>
          </td>
        </tr>
      )}
    </>
  );
}
