import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, getProcessingSettings, updateProcessingSettings } from "../api";
import { useAuth } from "../auth/AuthContext";
import { LoadingView } from "../components/LoadingView";
import { type TranslationKey, useI18n } from "../i18n";
import type { ProcessingSettings } from "../types";

type SwitchKey = keyof ProcessingSettings;

interface SettingRow {
  id: string;
  label: TranslationKey;
  help: TranslationKey;
  /** null for transcription, which runs for every recording and has no switch */
  key: SwitchKey | null;
}

const ROWS: SettingRow[] = [
  {
    id: "transcription",
    label: "settings.transcription",
    help: "settings.transcriptionHelp",
    key: null,
  },
  { id: "analysis", label: "settings.analysis", help: "settings.analysisHelp", key: "auto_analysis" },
  {
    id: "translation",
    label: "settings.translation",
    help: "settings.translationHelp",
    key: "auto_translation",
  },
];

interface Notice {
  kind: "saved" | "error";
  // A key rather than text, so the notice follows a language switch.
  message: TranslationKey;
}

export function SettingsPage() {
  const { t } = useI18n();
  const { invalidate } = useAuth();
  const navigate = useNavigate();
  const [settings, setSettings] = useState<ProcessingSettings | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saving, setSaving] = useState<SwitchKey | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  const signOut = useCallback(() => {
    invalidate();
    navigate("/login", { replace: true });
  }, [invalidate, navigate]);

  useEffect(() => {
    let cancelled = false;
    setLoadFailed(false);
    void getProcessingSettings()
      .then((loaded) => {
        if (!cancelled) setSettings(loaded);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) {
          signOut();
          return;
        }
        setLoadFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [loadAttempt, signOut]);

  async function toggle(key: SwitchKey, next: boolean) {
    setSaving(key);
    setNotice(null);
    try {
      // The answer carries every switch as it now stands, so a change made in
      // another tab meanwhile shows up here too.
      setSettings(await updateProcessingSettings({ [key]: next }));
      setNotice({ kind: "saved", message: "settings.saved" });
    } catch (caught: unknown) {
      if (caught instanceof ApiError && caught.status === 401) {
        signOut();
        return;
      }
      setNotice({
        kind: "error",
        message:
          caught instanceof ApiError && caught.status === 403
            ? "settings.authError"
            : "settings.saveError",
      });
    } finally {
      setSaving(null);
    }
  }

  if (loadFailed) {
    return (
      <section className="empty-state">
        <h1>{t("settings.loadErrorTitle")}</h1>
        <p>{t("settings.loadError")}</p>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => setLoadAttempt((current) => current + 1)}
        >
          {t("common.retry")}
        </button>
      </section>
    );
  }
  if (!settings) return <LoadingView label={t("settings.loading")} />;

  return (
    <section className="page-stack settings-page">
      <header className="page-heading">
        <div>
          <p className="eyebrow">{t("settings.eyebrow")}</p>
          <h1>{t("settings.title")}</h1>
          <p>{t("settings.description")}</p>
        </div>
      </header>

      {notice ? (
        <div
          className={`notice ${notice.kind === "error" ? "notice-error" : "notice-action"}`}
          role={notice.kind === "error" ? "alert" : "status"}
        >
          {t(notice.message)}
        </div>
      ) : null}

      <section aria-labelledby="settings-automation" className="panel">
        <p className="panel-kicker">{t("settings.automationKicker")}</p>
        <h2 id="settings-automation">{t("settings.automationTitle")}</h2>
        <ul className="settings-list">
          {ROWS.map((row) => {
            const key = row.key;
            const on = key === null || settings[key];
            const state =
              key === null
                ? t("settings.alwaysOn")
                : saving === key
                  ? t("settings.saving")
                  : t(on ? "settings.on" : "settings.off");
            return (
              <li className="setting-row" key={row.id}>
                <div className="setting-copy">
                  <span className="setting-name" id={`setting-${row.id}`}>
                    {t(row.label)}
                  </span>
                  <span className="setting-help" id={`setting-${row.id}-help`}>
                    {t(row.help)}
                  </span>
                </div>
                <label className="switch">
                  {/* Busy rather than disabled while a change is saved:
                      disabling the focused switch would throw keyboard focus
                      back to the top of the page. */}
                  <input
                    aria-describedby={`setting-${row.id}-help`}
                    aria-disabled={key !== null && saving !== null ? true : undefined}
                    aria-labelledby={`setting-${row.id}`}
                    checked={on}
                    className="visually-hidden"
                    disabled={key === null}
                    role="switch"
                    type="checkbox"
                    onChange={(event) => {
                      if (key !== null && saving === null) void toggle(key, event.target.checked);
                    }}
                  />
                  <span aria-hidden="true" className="switch-track">
                    <span className="switch-thumb" />
                  </span>
                  <span aria-hidden="true" className="switch-state">
                    {state}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
        <ul className="settings-notes">
          <li>{t("settings.noteTiming")}</li>
          <li>{t("settings.noteManual")}</li>
        </ul>
      </section>
    </section>
  );
}
