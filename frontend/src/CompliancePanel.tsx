import { useState } from "react";
import { api, COMPLIANCE_STATUSES, type Compliance, type Line } from "./api";

const OPEN = new Set(["required", "document_missing"]);

export function CompliancePanel({
  rows,
  lines,
  onSaved,
  onError,
}: {
  rows: Compliance[];
  lines: Line[];
  onSaved: () => void;
  onError: (msg: string) => void;
}) {
  const [saving, setSaving] = useState<number | null>(null);
  const open = rows.filter((r) => OPEN.has(r.status)).length;
  const lineSeq = new Map(lines.map((l) => [l.id, l.line_seq]));

  const save = async (row: Compliance, patch: Parameters<typeof api.saveCompliance>[1]) => {
    setSaving(row.id);
    try {
      await api.saveCompliance(row.id, patch);
      onSaved();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSaving(null);
    }
  };

  return (
    <section className="panel" aria-labelledby="compliance-h">
      <div className="panel-head">
        <h3 id="compliance-h">Compliance</h3>
        {rows.length === 0 ? (
          <span className="muted">No requirements found in this RFQ</span>
        ) : (
          <span className={open ? "count-open" : "muted"}>
            {open ? `${open} open requirement${open === 1 ? "" : "s"}` : "All requirements resolved"}
          </span>
        )}
      </div>
      {rows.length > 0 && (
        <div className="table-scroll">
          <table className="compliance">
            <thead>
              <tr>
                <th>Requirement</th>
                <th>Type</th>
                <th>Applies to</th>
                <th>Source</th>
                <th>Buyer message</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td title={r.source_text ?? undefined}>
                    <strong>{r.requirement_label}</strong>
                    {r.required_document_type && <div className="muted small">Doc: {r.required_document_type}</div>}
                  </td>
                  <td className="mono">{r.requirement_code}</td>
                  <td className="muted">{r.line_id ? `Line ${lineSeq.get(r.line_id) ?? r.line_id}` : "Whole quote"}</td>
                  <td className="muted">{r.source}</td>
                  <td>
                    <input
                      aria-label={`Buyer message for ${r.requirement_label}`}
                      defaultValue={r.buyer_message ?? ""}
                      placeholder="—"
                      disabled={saving === r.id}
                      onBlur={(e) => {
                        const v = e.target.value.trim() || null;
                        if (v !== (r.buyer_message ?? null)) save(r, { buyer_message: v });
                      }}
                    />
                  </td>
                  <td>
                    <select
                      aria-label={`Status for ${r.requirement_label}`}
                      className={OPEN.has(r.status) ? "status-select open" : "status-select"}
                      value={r.status}
                      disabled={saving === r.id}
                      onChange={(e) => save(r, { status: e.target.value as Compliance["status"] })}
                    >
                      {COMPLIANCE_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s.replace(/_/g, " ")}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
