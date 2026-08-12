import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, listRecordings } from "../api";
import { useAuth } from "../auth/AuthContext";
import { LoadingView } from "../components/LoadingView";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatDateTime, formatDuration } from "../format";
import { statusLabelKey, useI18n } from "../i18n";
import type { RecordingListResponse, RecordingStatus } from "../types";

const PAGE_SIZE = 20;
const validStatuses = new Set(["uploaded", "queued", "processing", "completed", "failed"]);

export function RecordingsPage() {
  const navigate = useNavigate();
  const { invalidate } = useAuth();
  const { locale, t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const offset = Math.max(0, Number(searchParams.get("offset")) || 0);
  const deviceId = searchParams.get("device_id") ?? "";
  const rawStatus = searchParams.get("status") ?? "";
  const status = validStatuses.has(rawStatus) ? (rawStatus as RecordingStatus) : "";
  const [deviceInput, setDeviceInput] = useState(deviceId);
  const [statusInput, setStatusInput] = useState<RecordingStatus | "">(status);
  const [data, setData] = useState<RecordingListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void listRecordings({ limit: PAGE_SIZE, offset, deviceId, status })
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) {
          invalidate();
          navigate("/login", { replace: true });
          return;
        }
        setError(t("recordings.error"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [deviceId, invalidate, navigate, offset, status, t]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = new URLSearchParams();
    if (deviceInput.trim()) next.set("device_id", deviceInput.trim());
    if (statusInput) next.set("status", statusInput);
    setSearchParams(next);
  }

  function goToOffset(nextOffset: number) {
    const next = new URLSearchParams(searchParams);
    if (nextOffset > 0) next.set("offset", String(nextOffset));
    else next.delete("offset");
    setSearchParams(next);
  }

  const pageItems = data?.items ?? [];
  const activeCount = pageItems.filter((item) =>
    ["uploaded", "queued", "processing"].includes(item.processing_status),
  ).length;
  const completedCount = pageItems.filter(
    (item) => item.processing_status === "completed",
  ).length;

  return (
    <section className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">{t("recordings.eyebrow")}</p>
          <h1>{t("recordings.title")}</h1>
          <p>{t("recordings.description")}</p>
        </div>
        <div className="privacy-note">
          <span className="privacy-icon" aria-hidden="true">⌁</span>
          <span>
            <strong>{t("recordings.privacyTitle")}</strong>
            <small>{t("recordings.privacyDescription")}</small>
          </span>
        </div>
      </header>

      <div className="archive-stats" aria-label={t("recordings.summaryAria")}>
        <div>
          <span>{t("recordings.visible")}</span>
          <strong>{loading ? "—" : pageItems.length.toString().padStart(2, "0")}</strong>
          <small>{t("recordings.visibleSub")}</small>
        </div>
        <div>
          <span>{t("recordings.active")}</span>
          <strong>{loading ? "—" : activeCount.toString().padStart(2, "0")}</strong>
          <small>{t("recordings.activeSub")}</small>
        </div>
        <div>
          <span>{t("recordings.completed")}</span>
          <strong>{loading ? "—" : completedCount.toString().padStart(2, "0")}</strong>
          <small>{t("recordings.completedSub")}</small>
        </div>
        <div className="archive-pulse" aria-hidden="true">
          <span>{t("recordings.live")}</span>
          <div><i /><i /><i /><i /><i /><i /><i /></div>
        </div>
      </div>

      <form className="filter-bar" onSubmit={applyFilters}>
        <div className="filter-title">
          <span>{t("recordings.filter")}</span>
          <strong>{t("recordings.organize")}</strong>
        </div>
        <label>
          {t("recordings.deviceId")}
          <input
            name="device-id"
            placeholder={t("recordings.devicePlaceholder")}
            value={deviceInput}
            onChange={(event) => setDeviceInput(event.target.value)}
          />
        </label>
        <label>
          {t("recordings.status")}
          <select
            name="status"
            value={statusInput}
            onChange={(event) => setStatusInput(event.target.value as RecordingStatus | "")}
          >
            <option value="">{t("recordings.allStatuses")}</option>
            <option value="queued">{t(statusLabelKey("queued"))}</option>
            <option value="processing">{t(statusLabelKey("processing"))}</option>
            <option value="completed">{t(statusLabelKey("completed"))}</option>
            <option value="failed">{t(statusLabelKey("failed"))}</option>
            <option value="uploaded">{t(statusLabelKey("uploaded"))}</option>
          </select>
        </label>
        <button className="button button-secondary" type="submit">
          {t("recordings.apply")}
        </button>
      </form>

      {loading ? <LoadingView label={t("recordings.loading")} /> : null}
      {error ? (
        <div className="notice notice-error" role="alert">
          {error}
        </div>
      ) : null}
      {!loading && !error && data?.items.length === 0 ? (
        <div className="empty-state">
          <span className="empty-wave" aria-hidden="true" />
          <h2>{t("recordings.emptyTitle")}</h2>
          <p>{deviceId || status ? t("recordings.emptyFiltered") : t("recordings.emptyDefault")}</p>
        </div>
      ) : null}
      {!loading && data && data.items.length > 0 ? (
        <div className="recording-grid" aria-live="polite">
          {data.items.map((recording, index) => (
            <article className="recording-card" key={recording.id}>
              <div className="recording-index" aria-hidden="true">
                {String(offset + index + 1).padStart(2, "0")}
              </div>
              <div className="recording-identity">
                <div className="recording-card-top">
                  <StatusBadge status={recording.processing_status} />
                  <span>{formatDateTime(recording.started_at, locale)}</span>
                </div>
                <h2>
                  <Link to={`/recordings/${recording.id}`}>{recording.original_filename}</Link>
                </h2>
                <p className="device-id">{t("recordings.devicePrefix")} / {recording.device_id}</p>
              </div>
              <dl className="recording-facts">
                <div>
                  <dt>{t("recordings.duration")}</dt>
                  <dd>{formatDuration(recording.duration_seconds, locale)}</dd>
                </div>
                <div>
                  <dt>{t("recordings.format")}</dt>
                  <dd>{recording.audio_format.toUpperCase()}</dd>
                </div>
                <div>
                  <dt>{t("recordings.size")}</dt>
                  <dd>{formatBytes(recording.file_size)}</dd>
                </div>
              </dl>
              <Link className="card-link" to={`/recordings/${recording.id}`}>
                <span className="card-link-label">{t("recordings.open")}</span>
                <span className="card-link-arrow" aria-hidden="true">↗</span>
              </Link>
            </article>
          ))}
        </div>
      ) : null}

      {!loading && data && (offset > 0 || data.items.length === PAGE_SIZE) ? (
        <nav className="pagination" aria-label={t("recordings.pagination")}>
          <button
            className="button button-secondary"
            disabled={offset === 0}
            type="button"
            onClick={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            {t("recordings.previous")}
          </button>
          <span>{t("recordings.page", { page: Math.floor(offset / PAGE_SIZE) + 1 })}</span>
          <button
            className="button button-secondary"
            disabled={data.items.length < PAGE_SIZE}
            type="button"
            onClick={() => goToOffset(offset + PAGE_SIZE)}
          >
            {t("recordings.next")}
          </button>
        </nav>
      ) : null}
    </section>
  );
}
