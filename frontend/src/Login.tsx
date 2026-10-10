import { useEffect, useRef, useState } from "react";
import { api, type Config, type User } from "./api";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (opts: { client_id: string; callback: (r: { credential: string }) => void }) => void;
          renderButton: (el: HTMLElement, opts: Record<string, unknown>) => void;
        };
      };
    };
  }
}

export function Login({ config, onSignedIn }: { config: Config; onSignedIn: (u: User) => void }) {
  const buttonRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");

  useEffect(() => {
    const clientId = config.google_client_id;
    if (!clientId) return;
    // The GSI script loads async; wait for it before rendering the button.
    const timer = setInterval(() => {
      if (!window.google || !buttonRef.current) return;
      clearInterval(timer);
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: ({ credential }) =>
          api
            .loginGoogle(credential)
            .then(onSignedIn)
            .catch((e) => setError(e.message)),
      });
      window.google.accounts.id.renderButton(buttonRef.current, {
        theme: "outline",
        size: "large",
        text: "continue_with",
      });
    }, 100);
    return () => clearInterval(timer);
  }, [config.google_client_id, onSignedIn]);

  return (
    <div className="center">
      <div className="card login">
        <h1>Line Item Review</h1>
        <p className="muted">Sign in with any Google account — first sign-in creates your account.</p>
        {config.google_client_id ? (
          <div ref={buttonRef} />
        ) : (
          <p className="warn">GOOGLE_CLIENT_ID is not set on the server.</p>
        )}
        {config.dev_auth && (
          <form
            className="dev-login"
            onSubmit={(e) => {
              e.preventDefault();
              api
                .loginDev(email)
                .then(onSignedIn)
                .catch((err) => setError(err.message));
            }}
          >
            <label className="muted">Dev sign-in (REVIEW_DEV_AUTH=1)</label>
            <input
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
            <button type="submit">Sign in</button>
          </form>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}
