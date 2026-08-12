import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api";
import { useAuth } from "../auth/AuthContext";
import { LanguageSwitch } from "../components/LanguageSwitch";
import { useI18n } from "../i18n";

export function SetupPage() {
  const { setup, setupEnabled } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const [setupToken, setSetupToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError(t("setup.passwordMismatch"));
      return;
    }
    if (password.length < 12) {
      setError(t("setup.passwordTooShort"));
      return;
    }
    setSubmitting(true);
    try {
      await setup(setupToken.trim(), username.trim(), password);
      navigate("/login", { replace: true });
    } catch (caught) {
      if (caught instanceof ApiError) {
        if (caught.status === 401 || caught.status === 403) setError(t("setup.invalidToken"));
        else if (caught.status === 409) setError(t("setup.alreadyComplete"));
        else setError(caught.message);
      } else {
        setError(t("setup.failed"));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="centered-page auth-page">
      <div className="auth-language"><LanguageSwitch /></div>
      <section className="auth-intro">
        <div className="auth-brand-lockup">
          <span className="brand-mark brand-mark-large" aria-hidden="true">
            <i />
            <i />
            <i />
            <i />
            <i />
          </span>
          <span>
            <strong>Wave Archive</strong>
            <small>{t("product.tagline")}</small>
          </span>
        </div>
        <p className="eyebrow">{t("setup.eyebrow")}</p>
        <h1>{t("setup.heroLine1")}<br />{t("setup.heroLine2")}</h1>
        <p>{t("setup.description")}</p>
        <ul className="trust-list">
          <li>{t("setup.trust1")}</li>
          <li>{t("setup.trust2")}</li>
          <li>{t("setup.trust3")}</li>
        </ul>
      </section>
      <section className="auth-card">
        <p className="step-label">{t("setup.step")}</p>
        <h2>{t("setup.title")}</h2>
        {!setupEnabled ? (
          <p className="form-error" role="alert">
            {t("setup.disabled")}
          </p>
        ) : null}
        <form onSubmit={(event) => void handleSubmit(event)}>
          <label>
            {t("setup.token")}
            <input
              autoComplete="one-time-code"
              name="setup-token"
              required
              disabled={!setupEnabled}
              type="password"
              value={setupToken}
              onChange={(event) => setSetupToken(event.target.value)}
            />
            <small>{t("setup.tokenHint")}</small>
          </label>
          <label>
            {t("setup.username")}
            <input
              autoComplete="username"
              maxLength={128}
              name="username"
              required
              disabled={!setupEnabled}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label>
            {t("setup.password")}
            <input
              autoComplete="new-password"
              minLength={12}
              name="password"
              required
              disabled={!setupEnabled}
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <small>{t("setup.passwordHint")}</small>
          </label>
          <label>
            {t("setup.confirmPassword")}
            <input
              autoComplete="new-password"
              minLength={12}
              name="confirm-password"
              required
              disabled={!setupEnabled}
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
          </label>
          {error ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : null}
          <button
            className="button button-primary button-full"
            disabled={submitting || !setupEnabled}
            type="submit"
          >
            {submitting ? t("setup.submitting") : t("setup.submit")}
          </button>
        </form>
      </section>
    </main>
  );
}
