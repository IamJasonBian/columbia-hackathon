import { useEffect, useState } from "react";
import { api, type QuoteSummary } from "./api";
import { LockIcon } from "./icons";

export function QuoteList({ selected, onSelect }: { selected: number | null; onSelect: (id: number) => void }) {
  const [query, setQuery] = useState("");
  const [quotes, setQuotes] = useState<QuoteSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api.quotes(query).then((rows) => {
        if (!cancelled) {
          setQuotes(rows);
          setLoading(false);
        }
      });
    const debounce = setTimeout(load, 200);
    const poll = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearTimeout(debounce);
      clearInterval(poll);
    };
  }, [query]);

  return (
    <div className="quote-list">
      <input
        aria-label="Search quotes"
        className="search"
        placeholder="Search RFQ #, subject, buyer"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {loading && <div className="muted pad">Loading…</div>}
      <ul>
        {quotes.map((q) => (
          <li key={q.id}>
            <button className={q.id === selected ? "quote active" : "quote"} onClick={() => onSelect(q.id)}>
              <span className="quote-title">{q.rfq_number ?? `Quote ${q.id}`}</span>
              <span className="quote-sub">{q.email_subject}</span>
              <span className="quote-meta">
                <span className={`pill status-${q.workflow_status}`}>{q.workflow_status}</span>
                <span>{q.line_count} lines</span>
                {q.locked_count > 0 && (
                  <span className="pill locked">
                    <LockIcon /> {q.locked_count}
                  </span>
                )}
                {q.open_compliance_count > 0 && (
                  <span className="pill cert">
                    {q.open_compliance_count} cert{q.open_compliance_count === 1 ? "" : "s"}
                  </span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
