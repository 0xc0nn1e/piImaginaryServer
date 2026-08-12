import type { PropsWithChildren } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { useI18n } from "../i18n";
import { LoadingView } from "./LoadingView";

export function RequireAuth({ children }: PropsWithChildren) {
  const { t } = useI18n();
  const { loading, setupRequired, user, bootstrapError, refresh } = useAuth();
  const location = useLocation();

  if (loading) return <LoadingView label={t("common.checkingLogin")} />;
  if (bootstrapError) {
    return (
      <main className="centered-page">
        <section className="auth-card" aria-live="polite">
          <p className="eyebrow">{t("common.connectionIssue")}</p>
          <h1>{t("common.cannotOpen")}</h1>
          <p>{bootstrapError}</p>
          <button className="button button-primary" type="button" onClick={() => void refresh()}>
            {t("common.retry")}
          </button>
        </section>
      </main>
    );
  }
  if (setupRequired) return <Navigate to="/setup" replace />;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return children;
}

export function SetupGuard({ children }: PropsWithChildren) {
  const { t } = useI18n();
  const { loading, setupRequired, user } = useAuth();
  if (loading) return <LoadingView label={t("common.checkingSetup")} />;
  if (!setupRequired) return <Navigate to={user ? "/recordings" : "/login"} replace />;
  return children;
}

export function LoginGuard({ children }: PropsWithChildren) {
  const { t } = useI18n();
  const { loading, setupRequired, user } = useAuth();
  if (loading) return <LoadingView label={t("common.checkingLogin")} />;
  if (setupRequired) return <Navigate to="/setup" replace />;
  if (user) return <Navigate to="/recordings" replace />;
  return children;
}

export function HomeRedirect() {
  const { loading, setupRequired, user } = useAuth();
  if (loading) return <LoadingView />;
  if (setupRequired) return <Navigate to="/setup" replace />;
  return <Navigate to={user ? "/recordings" : "/login"} replace />;
}
