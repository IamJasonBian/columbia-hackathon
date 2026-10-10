import { useEffect, useRef, useState } from "react";
import { api, type Config, type User } from "./api";
import { LockIcon, Logo } from "./icons";

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

/** Signed-out home: what the tool does, plus the sign-in form. */
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
    <div className="landing">
      <section className="landing-brand">
        <div className="brand">
          <Logo />
          <span>Line Item Review</span>
        </div>
        <div className="landing-pitch">
          <h1>Price every RFQ line without stepping on each other.</h1>
          <p>
            Incoming RFQs are parsed into line items and buyer compliance requirements. Click a row to lock it while you price
            it — your teammates see who has it until you save or step away.
          </p>
        </div>
        <ul className="legend">
          <li>
            <span className="swatch mine" /> Blue — you're editing
          </li>
          <li>
            <span className="swatch locked" /> Amber — a teammate has it
          </li>
          <li>
            <span className="swatch open" /> Open — click to take it
          </li>
        </ul>
        <div className="preview" aria-hidden="true">
          <div className="preview-row">
            <span className="mono">1</span>
            <span>Medical mask, Type II, EN 14683, ear loops</span>
          </div>
          <div className="preview-row mine">
            <span className="mono">2</span>
            <span>Medical mask, Type II, EN 14683, ties</span>
            <span className="pill mine">Editing · you</span>
          </div>
          <div className="preview-row locked">
            <span className="mono">3</span>
            <span>Sample container, 60 mL, screw cap</span>
            <span className="pill locked">
              <LockIcon /> Teammate
            </span>
          </div>
        </div>
      </section>
      <section className="landing-form">
        <div className="signin">
          <div>
            <h2>Sign in</h2>
            <p className="muted">First sign-in creates your account.</p>
          </div>
          {config.google_client_id && <div ref={buttonRef} />}
          {config.dev_auth && (
            <form
              className={config.google_client_id ? "signin-form divided" : "signin-form"}
              onSubmit={(e) => {
                e.preventDefault();
                api
                  .loginDev(email)
                  .then(onSignedIn)
                  .catch((err) => setError(err.message));
              }}
            >
              <label htmlFor="email">{config.google_client_id ? "Or use your email" : "Work email"}</label>
              <input
                id="email"
                type="email"
                placeholder="you@company.com"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
              <button type="submit" className="primary">
                Continue
              </button>
            </form>
          )}
          {!config.google_client_id && !config.dev_auth && <p className="warn">Sign-in is not configured on this server.</p>}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <p className="fineprint muted">
            Edits write straight to the shared quote database. Row locks release after {config.lock_ttl_seconds} seconds idle.
          </p>
        </div>
      </section>
    </div>
  );
}
