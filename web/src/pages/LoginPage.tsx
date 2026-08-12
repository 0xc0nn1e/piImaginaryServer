import { type FormEvent, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api";
import { useAuth } from "../auth/AuthContext";
import { LanguageSwitch } from "../components/LanguageSwitch";
import { useI18n } from "../i18n";

export function LoginPage() {
  const { login } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(username.trim(), password);
      const state = location.state as { from?: string } | null;
      const destination = safeRedirectPath(state?.from);
      navigate(destination, { replace: true });
    } catch (caught) {
      setError(
        caught instanceof ApiError && (caught.status === 401 || caught.status === 403)
          ? t("login.invalid")
          : t("login.failed"),
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="centered-page login-page">
      <div className="auth-language"><LanguageSwitch /></div>
      <section className="login-brand">
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
        <p className="eyebrow">{t("login.eyebrow")}</p>
        <h1>{t("login.heroLine1")}<br />{t("login.heroLine2")}</h1>
        <p>{t("login.description")}</p>
        <div className="auth-signal" aria-hidden="true">
          <span>REC</span>
          <div><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
          <small>16 KHZ / PRIVATE</small>
        </div>
      </section>
      <section className="auth-card login-card">
        <p className="step-label">{t("login.secureAccess")}</p>
        <h2>{t("login.title")}</h2>
        <p className="muted">{t("login.hint")}</p>
        <form onSubmit={(event) => void handleSubmit(event)}>
          <label>
            {t("login.username")}
            <input
              autoComplete="username"
              name="username"
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label>
            {t("login.password")}
            <input
              autoComplete="current-password"
              name="password"
              required
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {error ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : null}
          <button className="button button-primary button-full" disabled={submitting} type="submit">
            {submitting ? t("login.submitting") : t("login.submit")}
          </button>
        </form>
        <p className="auth-footnote">{t("login.sessionNote")}</p>
      </section>
    </main>
  );
}

export function safeRedirectPath(value: unknown): string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : "/recordings";
}
