import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  deleteRecording,
  getActivity,
  getRecording,
  getRecordingStatus,
  getTranscript,
  reprocessRecording,
} from "../api";
import { useAuth } from "../auth/AuthContext";
import { LoadingView } from "../components/LoadingView";
import { StatusBadge } from "../components/StatusBadge";
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatTimestamp,
} from "../format";
import { activityLabelKey, stageLabelKey, statusLabelKey, useI18n } from "../i18n";
import type {
  ActivityResponse,
  RecordingStatusResponse,
  RecordingSummary,
  TranscriptResponse,
} from "../types";

type DetailTab = "overview" | "activity" | "transcript";
type TranscriptState =
  | { kind: "idle" | "loading" | "pending" }
  | { kind: "ready"; data: TranscriptResponse }
  | { kind: "error"; message: string };

const terminalStatuses = new Set(["completed", "failed"]);
const failureEvents = new Set(["processing_failed"]);

export function RecordingDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { invalidate } = useAuth();
  const { locale, t } = useI18n();
  const [tab, setTab] = useState<DetailTab>("overview");
  const [recording, setRecording] = useState<RecordingSummary | null>(null);
  const [status, setStatus] = useState<RecordingStatusResponse | null>(null);
  const [activity, setActivity] = useState<ActivityResponse | null>(null);
  const [transcript, setTranscript] = useState<TranscriptState>({ kind: "idle" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [mutationPending, setMutationPending] = useState<"reprocess" | "delete" | null>(
    null,
  );
  const [mutationMessage, setMutationMessage] = useState<string | null>(null);

  const handleRequestError = useCallback(
    (caught: unknown) => {
      if (caught instanceof ApiError && caught.status === 401) {
        invalidate();
        navigate("/login", { replace: true });
        return;
      }
      if (caught instanceof ApiError && caught.status === 404) {
        setError(t("detail.notFound"));
        return;
      }
      setError(t("detail.loadError"));
    },
    [invalidate, navigate, t],
  );

  const refreshLiveData = useCallback(async () => {
    const [nextStatus, nextActivity] = await Promise.all([
      getRecordingStatus(id),
      getActivity(id),
    ]);
    setStatus(nextStatus);
    setActivity(nextActivity);
    setRecording((current) =>
      current ? { ...current, processing_status: nextStatus.status } : current,
    );
    return nextStatus;
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void Promise.all([getRecording(id), getRecordingStatus(id), getActivity(id)])
      .then(([nextRecording, nextStatus, nextActivity]) => {
        if (cancelled) return;
        setRecording(nextRecording);
        setStatus(nextStatus);
        setActivity(nextActivity);
      })
      .catch((caught: unknown) => {
        if (!cancelled) handleRequestError(caught);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [handleRequestError, id]);

  useEffect(() => {
    if (!status || terminalStatuses.has(status.status)) return undefined;
    const interval = window.setInterval(() => {
      void refreshLiveData().catch(handleRequestError);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [handleRequestError, refreshLiveData, status]);

  useEffect(() => {
    if (tab !== "transcript") return;
    let cancelled = false;
    setTranscript({ kind: "loading" });
    void getTranscript(id)
      .then((data) => {
        if (!cancelled) setTranscript({ kind: "ready", data });
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 409) {
          setTranscript({ kind: "pending" });
        } else if (caught instanceof ApiError && caught.status === 401) {
          invalidate();
          navigate("/login", { replace: true });
        } else {
          setTranscript({ kind: "error", message: t("detail.transcriptError") });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [id, invalidate, navigate, status?.status, t, tab]);

  const sortedActivity = useMemo(
    () =>
      [...(activity?.items ?? [])].sort(
        (left, right) =>
          new Date(right.occurred_at).getTime() - new Date(left.occurred_at).getTime(),
      ),
    [activity],
  );

  async function copyTranscript() {
    if (transcript.kind !== "ready") return;
    await navigator.clipboard.writeText(transcript.data.text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  function handleMutationError(caught: unknown, fallback: string) {
    if (caught instanceof ApiError && caught.status === 401) {
      invalidate();
      navigate("/login", { replace: true });
      return;
    }
    if (caught instanceof ApiError && caught.status === 403) {
      setMutationMessage(t("detail.authError"));
      return;
    }
    if (caught instanceof ApiError && caught.status === 409) {
      setMutationMessage(t("detail.activeConflict"));
      return;
    }
    setMutationMessage(fallback);
  }

  async function handleReprocess() {
    setMutationPending("reprocess");
    setMutationMessage(null);
    try {
      await reprocessRecording(id);
      await refreshLiveData();
      setMutationMessage(t("detail.reprocessQueued"));
    } catch (caught: unknown) {
      handleMutationError(caught, t("detail.reprocessError"));
    } finally {
      setMutationPending(null);
    }
  }

  async function handleDelete() {
    const confirmed = window.confirm(
      t("detail.deleteConfirm", {
        filename: recording?.original_filename ?? t("detail.thisRecording"),
      }),
    );
    if (!confirmed) return;
    setMutationPending("delete");
    setMutationMessage(null);
    try {
      await deleteRecording(id);
      navigate("/recordings", { replace: true });
    } catch (caught: unknown) {
      handleMutationError(caught, t("detail.deleteError"));
      setMutationPending(null);
    }
  }

  if (loading) return <LoadingView label={t("detail.loading")} />;
  if (error || !recording || !status) {
    return (
      <section className="empty-state">
        <h1>{t("detail.cannotDisplay")}</h1>
        <p>{error ?? t("detail.missing")}</p>
        <Link className="button button-secondary" to="/recordings">
          {t("detail.back")}
        </Link>
      </section>
    );
  }

  return (
    <section className="page-stack detail-page">
      <Link className="back-link" to="/recordings">
        <span aria-hidden="true">←</span> {t("detail.back")}
      </Link>
      <header className="detail-heading">
        <div>
          <div className="detail-status-row">
            <StatusBadge status={status.status} />
            {!terminalStatuses.has(status.status) ? (
              <span className="live-indicator">{t("detail.live")}</span>
            ) : null}
          </div>
          <h1>{recording.original_filename}</h1>
          <p>{recording.device_id}</p>
        </div>
        <div className="detail-controls">
          <div className="detail-time">
            <span>{t("detail.started")}</span>
            <strong>{formatDateTime(recording.started_at, locale)}</strong>
            <small>{formatDuration(recording.duration_seconds, locale)}</small>
          </div>
          <div className="detail-actions">
            <button
              className="button button-secondary"
              disabled={!terminalStatuses.has(status.status) || mutationPending !== null}
              type="button"
              onClick={() => void handleReprocess()}
            >
              {mutationPending === "reprocess" ? t("detail.reprocessing") : t("detail.reprocess")}
            </button>
            <button
              className="button button-danger"
              disabled={!terminalStatuses.has(status.status) || mutationPending !== null}
              type="button"
              onClick={() => void handleDelete()}
            >
              {mutationPending === "delete" ? t("detail.deleting") : t("detail.delete")}
            </button>
          </div>
        </div>
      </header>

      {mutationMessage ? (
        <div className="notice notice-action" role="status">
          {mutationMessage}
        </div>
      ) : null}

      <div className="tabs" role="tablist" aria-label={t("detail.tabsAria")}>
        {([
          ["overview", t("detail.tabOverview")],
          ["activity", t("detail.tabActivity")],
          ["transcript", t("detail.tabTranscript")],
        ] as const).map(([value, label]) => (
          <button
            aria-controls={`panel-${value}`}
            aria-selected={tab === value}
            className={tab === value ? "active" : ""}
            id={`tab-${value}`}
            key={value}
            role="tab"
            type="button"
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "overview" ? (
        <div
          aria-labelledby="tab-overview"
          className="tab-panel overview-grid"
          id="panel-overview"
          role="tabpanel"
        >
          <section className="panel">
            <p className="panel-kicker">{t("detail.fileData")}</p>
            <h2>{t("detail.recordingInfo")}</h2>
            <dl className="detail-list">
              <div>
                <dt>{t("detail.recordingTime")}</dt>
                <dd>{formatDateTime(recording.started_at, locale)}</dd>
              </div>
              <div>
                <dt>{t("detail.endedTime")}</dt>
                <dd>{formatDateTime(recording.ended_at, locale)}</dd>
              </div>
              <div>
                <dt>{t("detail.format")}</dt>
                <dd>{recording.audio_format.toUpperCase()}</dd>
              </div>
              <div>
                <dt>{t("detail.fileSize")}</dt>
                <dd>{formatBytes(recording.file_size)}</dd>
              </div>
              <div>
                <dt>{t("detail.sampleRate")}</dt>
                <dd>{recording.sample_rate ? `${recording.sample_rate.toLocaleString()} Hz` : "—"}</dd>
              </div>
              <div>
                <dt>{t("detail.channels")}</dt>
                <dd>{recording.channels ?? "—"}</dd>
              </div>
            </dl>
          </section>
          <section className="panel stage-panel">
            <p className="panel-kicker">{t("detail.backgroundProcessing")}</p>
            <h2>{status.job ? t(stageLabelKey(status.job.stage)) : t("detail.waitingJobData")}</h2>
            {status.job ? (
              <>
                <div className="progress-track" aria-hidden="true">
                  <span className={`progress-${status.job.stage}`} />
                </div>
                <dl className="detail-list compact">
                  <div>
                    <dt>{t("detail.jobStatus")}</dt>
                    <dd>{t(statusLabelKey(status.job.status))}</dd>
                  </div>
                  <div>
                    <dt>{t("detail.attempts")}</dt>
                    <dd>
                      {status.job.attempt_count} / {status.job.max_attempts}
                    </dd>
                  </div>
                  <div>
                    <dt>{t("detail.processingStarted")}</dt>
                    <dd>{formatDateTime(status.job.started_at, locale)}</dd>
                  </div>
                </dl>
                {status.job.error?.message ? (
                  <div className="notice notice-error" role="status">
                    <strong>{t("detail.processingFailed")}</strong>
                    <span>{status.job.error.message}</span>
                  </div>
                ) : null}
              </>
            ) : (
              <p className="muted">{t("detail.noJob")}</p>
            )}
          </section>
        </div>
      ) : null}

      {tab === "activity" ? (
        <section
          aria-labelledby="tab-activity"
          className="tab-panel panel"
          id="panel-activity"
          role="tabpanel"
        >
          <div className="panel-heading-row">
            <div>
              <p className="panel-kicker">{t("detail.auditTrail")}</p>
              <h2>{t("detail.activityTitle")}</h2>
            </div>
            <span className="muted">{t("detail.safeEventsOnly")}</span>
          </div>
          {sortedActivity.length === 0 ? (
            <div className="inline-empty">{t("detail.noEvents")}</div>
          ) : (
            <ol className="timeline">
              {sortedActivity.map((item) => (
                <li key={item.id}>
                  <span
                    className={`timeline-dot status-${item.job_status ?? "unknown"}`}
                    aria-hidden="true"
                  />
                  <div>
                    <div className="timeline-heading">
                      <strong>{t(activityLabelKey(item.event_type))}</strong>
                      <time dateTime={item.occurred_at}>{formatDateTime(item.occurred_at, locale)}</time>
                    </div>
                    <p>
                      {item.stage ? t(stageLabelKey(item.stage)) : t("detail.unknownStage")} ·{" "}
                      {item.job_status ? t(statusLabelKey(item.job_status)) : t("detail.unknownStatus")} ·{" "}
                      {t("detail.attemptCount", {
                        attempt: item.attempt_count,
                        max: item.max_attempts,
                      })}
                    </p>
                    {item.retry_scheduled && item.next_attempt_at ? (
                      <p className="retry-note">
                        {t("detail.nextAttempt", {
                          time: formatDateTime(item.next_attempt_at, locale),
                        })}
                      </p>
                    ) : null}
                    {failureEvents.has(item.event_type) && item.message ? (
                      <p className="safe-error-message">{item.message}</p>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </section>
      ) : null}

      {tab === "transcript" ? (
        <section
          aria-labelledby="tab-transcript"
          className="tab-panel panel transcript-panel"
          id="panel-transcript"
          role="tabpanel"
        >
          <div className="panel-heading-row">
            <div>
              <p className="panel-kicker">{t("detail.transcriptKicker")}</p>
              <h2>{t("detail.transcriptTitle")}</h2>
            </div>
            {transcript.kind === "ready" && transcript.data.segments.length > 0 ? (
              <button className="button button-secondary" type="button" onClick={() => void copyTranscript()}>
                {copied ? t("detail.copied") : t("detail.copyAll")}
              </button>
            ) : null}
          </div>
          {transcript.kind === "loading" || transcript.kind === "idle" ? (
            <LoadingView label={t("detail.transcriptLoading")} />
          ) : null}
          {transcript.kind === "pending" ? (
            <div className="empty-state compact-empty">
              <h3>{t("detail.transcriptPending")}</h3>
              <p>{t("detail.transcriptPendingDescription")}</p>
            </div>
          ) : null}
          {transcript.kind === "error" ? (
            <div className="notice notice-error" role="alert">
              {transcript.message}
            </div>
          ) : null}
          {transcript.kind === "ready" && transcript.data.segments.length === 0 ? (
            <div className="empty-state compact-empty">
              <h3>{t("detail.noSpeech")}</h3>
              <p>{t("detail.noSpeechDescription")}</p>
            </div>
          ) : null}
          {transcript.kind === "ready" && transcript.data.segments.length > 0 ? (
            <ol className="transcript-list">
              {transcript.data.segments.map((segment) => (
                <li key={segment.sequence}>
                  <div className="segment-meta">
                    <time>{formatTimestamp(segment.start_time)}</time>
                    <strong>{segment.speaker_label}</strong>
                    {segment.language ? <span>{segment.language.toUpperCase()}</span> : null}
                    {segment.has_overlap ? <span className="overlap-label">{t("detail.overlap")}</span> : null}
                  </div>
                  <p>{segment.text}</p>
                </li>
              ))}
            </ol>
          ) : null}
        </section>
      ) : null}
    </section>
  );
}
