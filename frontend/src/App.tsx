import { useEffect, useState } from "react";
import { api, ApiError, type Config, type User } from "./api";
import { Login } from "./Login";
import { QuoteList } from "./QuoteList";
import { QuoteView } from "./QuoteView";

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [quoteId, setQuoteId] = useState<number | null>(() => {
    const id = Number(new URLSearchParams(location.search).get("quote"));
    return Number.isFinite(id) && id > 0 ? id : null;
  });

  useEffect(() => {
    api.config().then(setConfig);
    api
      .me()
      .then(setUser)
      .catch((e) => (e instanceof ApiError && e.status === 401 ? setUser(null) : Promise.reject(e)));
  }, []);

  useEffect(() => {
    const url = new URL(location.href);
    if (quoteId) url.searchParams.set("quote", String(quoteId));
    else url.searchParams.delete("quote");
    history.replaceState(null, "", url);
  }, [quoteId]);

  if (!config || user === undefined) return <div className="center muted">Loading…</div>;
  if (!user) return <Login config={config} onSignedIn={setUser} />;

  return (
    <div className="shell">
      <header className="topbar">
        <strong>Line Item Review</strong>
        <div className="me">
          {user.picture && <img src={user.picture} alt="" referrerPolicy="no-referrer" />}
          <span>{user.name ?? user.email}</span>
          <button
            className="ghost"
            onClick={async () => {
              await api.logout();
              setUser(null);
            }}
          >
            Sign out
          </button>
        </div>
      </header>
      <aside className="sidebar">
        <QuoteList selected={quoteId} onSelect={setQuoteId} />
      </aside>
      <main className="content">
        {quoteId ? (
          <QuoteView key={quoteId} quoteId={quoteId} me={user} ttlSeconds={config.lock_ttl_seconds} />
        ) : (
          <div className="center muted">Pick a quote to review its line items.</div>
        )}
      </main>
    </div>
  );
}
