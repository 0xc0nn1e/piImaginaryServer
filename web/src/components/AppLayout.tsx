import { useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { useI18n } from "../i18n";
import { LanguageSwitch } from "./LanguageSwitch";

export function AppLayout() {
  const { user, logout } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const [logoutError, setLogoutError] = useState(false);

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
        </nav>
        <LanguageSwitch />
        <div className="sidebar-status" aria-label={t("nav.serviceStatus")}>
          <span className="online-dot" aria-hidden="true" />
          <span>
            {t("nav.privateServer")}
            <small>{t("nav.localWorkspace")}</small>
          </span>
        </div>
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
          <span>{t("footer.privacy")}</span>
        </footer>
      </div>
    </div>
  );
}
