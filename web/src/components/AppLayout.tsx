import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { useI18n } from "../i18n";
import { LanguageSwitch } from "./LanguageSwitch";

type HealthState = "checking" | "ok" | "fail";

const SHOW_HEALTH = import.meta.env.VITE_SHOW_HEALTH !== "false";
const HEALTHY_POLL_INTERVAL_MS = 180_000;
const FAILED_POLL_INTERVAL_MS = 5_000;

export function AppLayout() {
  const { user, logout } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const [logoutError, setLogoutError] = useState(false);
  const [health, setHealth] = useState<HealthState>("checking");

  useEffect(() => {
    if (!SHOW_HEALTH) return;

    let stopped = false;
    let timeoutId: number | undefined;
    let controller: AbortController | undefined;

    const checkHealth = async () => {
      let ok = false;
      controller = new AbortController();
      try {
        const response = await fetch("/health/ready", {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        const payload = (await response.json()) as { status?: string };
        ok = response.ok && payload.status === "ready";
        if (!stopped) setHealth(ok ? "ok" : "fail");
      } catch {
        if (!stopped) setHealth("fail");
      }
      if (!stopped) {
        timeoutId = window.setTimeout(
          () => void checkHealth(),
          ok ? HEALTHY_POLL_INTERVAL_MS : FAILED_POLL_INTERVAL_MS,
        );
      }
    };

    void checkHealth();
    return () => {
      stopped = true;
      controller?.abort();
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, []);

  const healthLabel = t(`health.${health}`);

  async function handleLogout() {
    try {
      setLogoutError(false);
      await logout();
      navigate("/login", { replace: true });
    } catch {
      setLogoutError(true);
    }
  }

  return (
    <div className="app-shell">
      <aside className="topbar">
        <Link className="brand" to="/recordings" aria-label={t("nav.home")}>
          <span className="brand-mark" aria-hidden="true">
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
        </Link>
        <nav aria-label={t("nav.main")}>
          <NavLink to="/recordings">
            <span className="nav-glyph" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span>
              {t("nav.recordings")}
              <small>{t("nav.recordingsSub")}</small>
            </span>
          </NavLink>
          <NavLink to="/days">
            <span className="nav-glyph" aria-hidden="true">
              <i />
              <i />
              <i />
              <i />
            </span>
            <span>
              {t("nav.days")}
              <small>{t("nav.daysSub")}</small>
            </span>
          </NavLink>
          <NavLink to="/queue">
            <span className="nav-glyph" aria-hidden="true">
              <i />
              <i />
            </span>
            <span>
              {t("nav.queue")}
              <small>{t("nav.queueSub")}</small>
            </span>
          </NavLink>
          <NavLink to="/bookmarks">
            <span className="nav-glyph" aria-hidden="true">
              <i />
              <i />
            </span>
            <span>
              {t("nav.bookmarks")}
              <small>{t("nav.bookmarksSub")}</small>
            </span>
          </NavLink>
        </nav>
        <LanguageSwitch />
        {SHOW_HEALTH ? (
          <div
            aria-label={t("nav.serviceStatus")}
            aria-live="polite"
            className="sidebar-status"
            role="status"
          >
            <span className={`health-dot health-${health}`} aria-hidden="true" />
            <span>
              {t("nav.privateServer")}
              <small>{healthLabel}</small>
            </span>
          </div>
        ) : null}
        <div className="account-menu">
          <span className="account-avatar" aria-hidden="true">
            {user?.username?.slice(0, 1).toUpperCase() ?? "A"}
          </span>
          <span className="account-name">
            {user?.username}
            <small>{t("nav.administrator")}</small>
          </span>
          <button
            aria-label={t("nav.logout")}
            className="button button-quiet account-logout"
            type="button"
            onClick={() => void handleLogout()}
          >
            <span aria-hidden="true">↗</span>
          </button>
        </div>
      </aside>
      <div className="app-workspace">
        <header className="mobile-header">
          <Link className="mobile-brand" to="/recordings">
            <span className="brand-mark" aria-hidden="true">
              <i />
              <i />
              <i />
              <i />
              <i />
            </span>
            <strong>Wave Archive</strong>
          </Link>
          <div className="mobile-actions">
            <LanguageSwitch compact />
            <button className="button button-quiet" type="button" onClick={() => void handleLogout()}>
              {t("nav.logout")}
            </button>
          </div>
        </header>
        <main className="main-content">
          {logoutError ? (
            <div className="notice notice-error logout-error" role="alert">
              {t("nav.logoutError")}
            </div>
          ) : null}
          <Outlet />
        </main>
        <footer className="footer">
          <span>{t("footer.tagline")}</span>
          <span className="footer-meta">
            <span>{t("footer.privacy")}</span>
            {SHOW_HEALTH ? (
              <span className="footer-health">
                <span className={`health-dot health-${health}`} aria-hidden="true" />
                {t("health.backend")}: {healthLabel}
              </span>
            ) : null}
          </span>
        </footer>
      </div>
    </div>
  );
}
