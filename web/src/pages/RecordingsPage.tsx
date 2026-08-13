import { type DragEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, listRecordings, uploadWebRecording } from "../api";
import { useAuth } from "../auth/AuthContext";
import { LoadingView } from "../components/LoadingView";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatDateTime, formatDuration } from "../format";
import { statusLabelKey, useI18n } from "../i18n";
import type { RecordingListResponse, RecordingStatus } from "../types";

const PAGE_SIZE = 20;
const validStatuses = new Set(["uploaded", "queued", "processing", "completed", "failed"]);

type UploadStatus = "waiting" | "uploading" | "queued" | "failed";
interface UploadItem {
  id: string;
  file: File;
  startedAt: string;
  status: UploadStatus;
  recordingId?: string;
  error?: string;
}

function localInputTime(timestamp: number): string {
  const date = new Date(timestamp);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

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
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
  }, [deviceId, invalidate, navigate, offset, refreshKey, status, t]);

  function addFiles(files: FileList | File[]) {
    const accepted = Array.from(files).filter((file) => /\.(mp3|wav)$/i.test(file.name));
    setUploadItems((current) => [
      ...current,
      ...accepted.map((file, index) => ({
        id: `${file.name}-${file.lastModified}-${file.size}-${current.length + index}`,
        file,
        startedAt: localInputTime(file.lastModified || Date.now()),
        status: "waiting" as const,
      })),
    ]);
    if (accepted.length) setUploadOpen(true);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    addFiles(event.dataTransfer.files);
  }

  async function runUploads(ids?: Set<string>) {
    if (uploading) return;
    setUploading(true);
    const queue = uploadItems.filter(
      (item) =>
        (ids ? ids.has(item.id) : item.status === "waiting" || item.status === "failed") &&
        item.status !== "queued",
    );
    for (const item of queue) {
      setUploadItems((current) =>
        current.map((candidate) =>
          candidate.id === item.id
            ? { ...candidate, status: "uploading", error: undefined }
            : candidate,
        ),
      );
      try {
        const startedAt = item.startedAt ? new Date(item.startedAt).toISOString() : undefined;
        const result = await uploadWebRecording(item.file, startedAt);
        setUploadItems((current) =>
          current.map((candidate) =>
            candidate.id === item.id
              ? {
                  ...candidate,
                  status: "queued",
                  recordingId: result.recording_id,
                  error: undefined,
                }
              : candidate,
          ),
        );
      } catch (caught: unknown) {
        if (caught instanceof ApiError && caught.status === 401) {
          invalidate();
          navigate("/login", { replace: true });
          break;
        }
        setUploadItems((current) =>
          current.map((candidate) =>
            candidate.id === item.id
              ? { ...candidate, status: "failed", error: t("upload.failed") }
              : candidate,
          ),
        );
      }
    }
    setUploading(false);
    setRefreshKey((current) => current + 1);
  }

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
        <div className="heading-actions">
          <button
            className="button button-primary"
            type="button"
            onClick={() => setUploadOpen((value) => !value)}
          >
            {t("upload.open")}
          </button>
          <div className="privacy-note">
          <span className="privacy-icon" aria-hidden="true">⌁</span>
          <span>
            <strong>{t("recordings.privacyTitle")}</strong>
            <small>{t("recordings.privacyDescription")}</small>
          </span>
          </div>
        </div>
      </header>

      {uploadOpen ? (
        <section className="panel upload-panel" aria-label={t("upload.title")}>
          <div className="panel-heading-row">
            <div>
              <p className="panel-kicker">MP3 / WAV</p>
              <h2>{t("upload.title")}</h2>
            </div>
            {uploadItems.length ? (
              <button
                className="button"
                disabled={uploading || uploadItems.every((item) => item.status === "queued")}
                type="button"
                onClick={() => void runUploads()}
              >
                {uploading ? t("upload.uploadingAll") : t("upload.start")}
              </button>
            ) : null}
          </div>
          <div
            className="upload-dropzone"
            onDragOver={(event) => event.preventDefault()}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              accept=".mp3,.wav,audio/mpeg,audio/wav"
              hidden
              multiple
              type="file"
              onChange={(event) => {
                if (event.target.files) addFiles(event.target.files);
                event.target.value = "";
              }}
            />
            <strong>{t("upload.drop")}</strong>
            <span>{t("upload.help")}</span>
          </div>
          {uploadItems.length ? (
            <ol className="upload-list">
              {uploadItems.map((item) => (
                <li key={item.id}>
                  <div>
                    <strong>{item.file.name}</strong>
                    <small>{formatBytes(item.file.size)}</small>
                  </div>
                  <label>
                    {t("upload.startedAt")}
                    <input
                      disabled={item.status === "uploading" || item.status === "queued"}
                      type="datetime-local"
                      value={item.startedAt}
                      onChange={(event) =>
                        setUploadItems((current) =>
                          current.map((candidate) =>
                            candidate.id === item.id
                              ? { ...candidate, startedAt: event.target.value }
                              : candidate,
                          ),
                        )
                      }
                    />
                  </label>
                  <span className={`upload-state upload-state-${item.status}`}>
                    {t(`upload.status.${item.status}` as Parameters<typeof t>[0])}
                  </span>
                  {item.error ? <small className="safe-error-message">{item.error}</small> : null}
                  {item.status === "failed" ? (
                    <button
                      className="button button-secondary"
                      disabled={uploading}
                      type="button"
                      onClick={() => void runUploads(new Set([item.id]))}
                    >
                      {t("common.retry")}
                    </button>
                  ) : null}
                  {item.recordingId ? (
                    <Link className="button button-secondary" to={`/recordings/${item.recordingId}`}>
                      {t("upload.openResult")}
                    </Link>
                  ) : null}
                </li>
              ))}
            </ol>
          ) : null}
        </section>
      ) : null}

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
