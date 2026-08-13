import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  deleteRecording,
  getActivity,
  getAnalysis,
  getReanalysis,
  getRecording,
  getRecordingAudioUrl,
  getRecordingStatus,
  getTranscript,
  reprocessRecording,
  updateAnalysis,
  updateTranscript,
} from "../api";
import { useAuth } from "../auth/AuthContext";
import { LoadingView } from "../components/LoadingView";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatDateTime, formatDuration, formatTimestamp } from "../format";
import { activityLabelKey, stageLabelKey, statusLabelKey, useI18n } from "../i18n";
import type {
  ActivityResponse,
  AnalysisResponse,
  AnalysisResultV2,
  RecordingStatusResponse,
  RecordingSummary,
  TranscriptResponse,
} from "../types";

type DetailTab = "overview" | "activity" | "transcript" | "analysis";
type TranscriptState =
  | { kind: "idle" | "loading" | "pending" }
  | { kind: "ready"; data: TranscriptResponse }
  | { kind: "error"; message: string };
type AnalysisState =
  | { kind: "idle" | "loading" | "pending" }
  | { kind: "ready"; data: AnalysisResponse }
  | { kind: "error"; message: string };
type Mutation = "reprocess" | "reanalyse" | "delete" | "transcript" | "analysis";

const terminalStatuses = new Set(["completed", "failed"]);
const failureEvents = new Set(["processing_failed"]);

function cloneResult(result: AnalysisResultV2): AnalysisResultV2 {
  const clone = JSON.parse(JSON.stringify(result)) as AnalysisResultV2;
  clone.summary ??= { ...clone.description };
  return clone;
}

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
  const [analysis, setAnalysis] = useState<AnalysisState>({ kind: "idle" });
  const [transcriptDraft, setTranscriptDraft] = useState<TranscriptResponse | null>(null);
  const [analysisDraft, setAnalysisDraft] = useState<AnalysisResultV2 | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [mutationPending, setMutationPending] = useState<Mutation | null>(null);
  const [mutationMessage, setMutationMessage] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const clipEndRef = useRef<number | null>(null);
  const [playingClip, setPlayingClip] = useState<string | null>(null);

  const stopPlayback = useCallback(() => {
    audioRef.current?.pause();
    clipEndRef.current = null;
    setPlayingClip(null);
  }, []);

  const playClip = useCallback(
    async (key: string, startTime: number, endTime: number | null) => {
      const audio = audioRef.current;
      if (!audio) return;
      if (playingClip === key && !audio.paused) {
        stopPlayback();
        return;
      }
      try {
        if (audio.readyState < HTMLMediaElement.HAVE_METADATA) {
          await new Promise<void>((resolve, reject) => {
            audio.addEventListener("loadedmetadata", () => resolve(), { once: true });
            audio.addEventListener("error", () => reject(new Error("audio unavailable")), {
              once: true,
            });
            audio.load();
          });
        }
        const fallbackEnd = Math.min(startTime + 8, audio.duration || startTime + 8);
        clipEndRef.current =
          endTime !== null && endTime > startTime ? endTime : fallbackEnd;
        audio.currentTime = Math.max(0, startTime);
        await audio.play();
        setPlayingClip(key);
      } catch {
        stopPlayback();
        setMutationMessage(t("analysis.playError"));
      }
    },
    [playingClip, stopPlayback, t],
  );

  useEffect(() => () => stopPlayback(), [stopPlayback]);

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

  const loadAnalysis = useCallback(async () => {
    setAnalysis({ kind: "loading" });
    try {
      setAnalysis({ kind: "ready", data: await getAnalysis(id) });
    } catch (caught: unknown) {
      if (caught instanceof ApiError && caught.status === 409) {
        setAnalysis({ kind: "pending" });
      } else if (caught instanceof ApiError && caught.status === 401) {
        invalidate();
        navigate("/login", { replace: true });
      } else {
        setAnalysis({ kind: "error", message: t("analysis.loadError") });
      }
    }
  }, [id, invalidate, navigate, t]);

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
    const analysisJobActive =
      status?.job?.kind === "analysis" &&
      (status.job.status === "queued" || status.job.status === "processing");
    if (!status || (terminalStatuses.has(status.status) && !analysisJobActive)) return undefined;
    const interval = window.setInterval(() => {
      void refreshLiveData()
        .then(() => {
          if (tab === "analysis") return loadAnalysis();
          return undefined;
        })
        .catch(handleRequestError);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [handleRequestError, loadAnalysis, refreshLiveData, status, tab]);

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

  useEffect(() => {
    if (tab === "analysis" && analysis.kind === "idle") void loadAnalysis();
  }, [analysis.kind, loadAnalysis, tab]);

  const sortedActivity = useMemo(
    () =>
      [...(activity?.items ?? [])].sort(
        (left, right) =>
          new Date(right.occurred_at).getTime() - new Date(left.occurred_at).getTime(),
      ),
    [activity],
  );

  async function copyText(key: string, text: string) {
    await navigator.clipboard.writeText(text);
    setCopied(key);
    window.setTimeout(() => setCopied(null), 1800);
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
      setMutationMessage(
        caught.code === "job_already_active"
          ? t("detail.activeConflict")
          : t("detail.revisionConflict"),
      );
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

  async function handleReanalyse() {
    setMutationPending("reanalyse");
    setMutationMessage(null);
    try {
      await getReanalysis(id);
      await refreshLiveData();
      await loadAnalysis();
      setMutationMessage(t("analysis.queued"));
    } catch (caught: unknown) {
      handleMutationError(caught, t("analysis.reprocessError"));
    } finally {
      setMutationPending(null);
    }
  }

  async function handleDelete() {
    if (
      !window.confirm(
        t("detail.deleteConfirm", {
          filename: recording?.original_filename ?? t("detail.thisRecording"),
        }),
      )
    )
      return;
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

  async function saveTranscript() {
    if (!transcriptDraft) return;
    setMutationPending("transcript");
    try {
      const saved = await updateTranscript(id, transcriptDraft);
      setTranscript({ kind: "ready", data: saved });
      setTranscriptDraft(null);
      setAnalysis({ kind: "idle" });
      setMutationMessage(t("detail.transcriptSaved"));
    } catch (caught: unknown) {
      handleMutationError(caught, t("detail.transcriptSaveError"));
    } finally {
      setMutationPending(null);
    }
  }

  async function saveAnalysis() {
    if (analysis.kind !== "ready" || !analysisDraft) return;
    setMutationPending("analysis");
    try {
      const saved = await updateAnalysis(id, analysis.data.revision, analysisDraft);
      setAnalysis({ kind: "ready", data: saved });
      setAnalysisDraft(null);
      setMutationMessage(t("analysis.saved"));
    } catch (caught: unknown) {
      handleMutationError(caught, t("analysis.saveError"));
    } finally {
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

  const shownTranscript = transcriptDraft ?? (transcript.kind === "ready" ? transcript.data : null);
  const shownAnalysis =
    analysisDraft ?? (analysis.kind === "ready" ? analysis.data.result : null);

  return (
    <section className="page-stack detail-page">
      <audio
        aria-hidden="true"
        className="clip-audio"
        onEnded={stopPlayback}
        onTimeUpdate={(event) => {
          const endTime = clipEndRef.current;
          if (endTime !== null && event.currentTarget.currentTime >= endTime) stopPlayback();
        }}
        preload="metadata"
        ref={audioRef}
        src={getRecordingAudioUrl(id)}
      />
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
              {mutationPending === "reprocess" ? t("detail.reprocessing") : t("detail.retranscribe")}
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

      {mutationMessage ? <div className="notice notice-action" role="status">{mutationMessage}</div> : null}

      <div className="tabs" role="tablist" aria-label={t("detail.tabsAria")}>
        {([
          ["overview", t("detail.tabOverview")],
          ["activity", t("detail.tabActivity")],
          ["transcript", t("detail.tabTranscript")],
          ["analysis", t("detail.tabAnalysis")],
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
        <div aria-labelledby="tab-overview" className="tab-panel overview-grid" id="panel-overview" role="tabpanel">
          <section className="panel">
            <p className="panel-kicker">{t("detail.fileData")}</p>
            <h2>{t("detail.recordingInfo")}</h2>
            <dl className="detail-list">
              <div><dt>{t("detail.recordingTime")}</dt><dd>{formatDateTime(recording.started_at, locale)}</dd></div>
              <div><dt>{t("detail.endedTime")}</dt><dd>{formatDateTime(recording.ended_at, locale)}</dd></div>
              <div><dt>{t("detail.format")}</dt><dd>{recording.audio_format.toUpperCase()}</dd></div>
              <div><dt>{t("detail.fileSize")}</dt><dd>{formatBytes(recording.file_size)}</dd></div>
              <div><dt>{t("detail.sampleRate")}</dt><dd>{recording.sample_rate ? `${recording.sample_rate.toLocaleString()} Hz` : "—"}</dd></div>
              <div><dt>{t("detail.channels")}</dt><dd>{recording.channels ?? "—"}</dd></div>
            </dl>
          </section>
          <section className="panel stage-panel">
            <p className="panel-kicker">{t("detail.backgroundProcessing")}</p>
            <h2>{status.job ? t(stageLabelKey(status.job.stage)) : t("detail.waitingJobData")}</h2>
            {status.job ? (
              <>
                <div className="progress-track" aria-hidden="true"><span className={`progress-${status.job.stage}`} /></div>
                <dl className="detail-list compact">
                  <div><dt>{t("detail.jobStatus")}</dt><dd>{t(statusLabelKey(status.job.status))}</dd></div>
                  <div><dt>{t("detail.jobKind")}</dt><dd>{status.job.kind === "analysis" ? t("detail.jobAnalysis") : t("detail.jobFull")}</dd></div>
                  <div><dt>{t("detail.attempts")}</dt><dd>{status.job.attempt_count} / {status.job.max_attempts}</dd></div>
                  <div><dt>{t("detail.processingStarted")}</dt><dd>{formatDateTime(status.job.started_at, locale)}</dd></div>
                </dl>
                {status.job.error?.message ? <div className="notice notice-error" role="status"><strong>{t("detail.processingFailed")}</strong><span>{status.job.error.message}</span></div> : null}
              </>
            ) : <p className="muted">{t("detail.noJob")}</p>}
          </section>
        </div>
      ) : null}

      {tab === "activity" ? (
        <section aria-labelledby="tab-activity" className="tab-panel panel" id="panel-activity" role="tabpanel">
          <div className="panel-heading-row"><div><p className="panel-kicker">{t("detail.auditTrail")}</p><h2>{t("detail.activityTitle")}</h2></div><span className="muted">{t("detail.safeEventsOnly")}</span></div>
          {sortedActivity.length === 0 ? <div className="inline-empty">{t("detail.noEvents")}</div> : (
            <ol className="timeline">{sortedActivity.map((item) => (
              <li key={item.id}>
                <span className={`timeline-dot status-${item.job_status ?? "unknown"}`} aria-hidden="true" />
                <div><div className="timeline-heading"><strong>{t(activityLabelKey(item.event_type))}</strong><time dateTime={item.occurred_at}>{formatDateTime(item.occurred_at, locale)}</time></div>
                  <p>{item.stage ? t(stageLabelKey(item.stage)) : t("detail.unknownStage")} · {item.job_status ? t(statusLabelKey(item.job_status)) : t("detail.unknownStatus")} · {item.job_kind === "analysis" ? t("detail.jobAnalysis") : t("detail.jobFull")} · {t("detail.attemptCount", { attempt: item.attempt_count, max: item.max_attempts })}</p>
                  {item.retry_scheduled && item.next_attempt_at ? <p className="retry-note">{t("detail.nextAttempt", { time: formatDateTime(item.next_attempt_at, locale) })}</p> : null}
                  {failureEvents.has(item.event_type) && item.message ? <p className="safe-error-message">{item.message}</p> : null}
                </div>
              </li>
            ))}</ol>
          )}
        </section>
      ) : null}

      {tab === "transcript" ? (
        <section aria-labelledby="tab-transcript" className="tab-panel panel transcript-panel" id="panel-transcript" role="tabpanel">
          <div className="panel-heading-row">
            <div><p className="panel-kicker">{t("detail.transcriptKicker")}</p><h2>{t("detail.transcriptTitle")}</h2></div>
            {shownTranscript?.segments.length ? <div className="inline-actions">
              {transcriptDraft ? <>
                <button className="button" disabled={mutationPending !== null} type="button" onClick={() => void saveTranscript()}>{mutationPending === "transcript" ? t("detail.saving") : t("detail.save")}</button>
                <button className="button button-secondary" type="button" onClick={() => setTranscriptDraft(null)}>{t("detail.cancel")}</button>
              </> : <>
                <button className="button button-secondary" type="button" onClick={() => setTranscriptDraft(structuredClone(shownTranscript))}>{t("detail.edit")}</button>
                <button className="button button-secondary" type="button" onClick={() => void copyText("transcript", shownTranscript.text)}>{copied === "transcript" ? t("detail.copied") : t("detail.copyAll")}</button>
              </>}
            </div> : null}
          </div>
          {transcript.kind === "loading" || transcript.kind === "idle" ? <LoadingView label={t("detail.transcriptLoading")} /> : null}
          {transcript.kind === "pending" ? <div className="empty-state compact-empty"><h3>{t("detail.transcriptPending")}</h3><p>{t("detail.transcriptPendingDescription")}</p></div> : null}
          {transcript.kind === "error" ? <div className="notice notice-error" role="alert">{transcript.message}</div> : null}
          {transcript.kind === "ready" && transcript.data.segments.length === 0 ? <div className="empty-state compact-empty"><h3>{t("detail.noSpeech")}</h3><p>{t("detail.noSpeechDescription")}</p></div> : null}
          {shownTranscript?.segments.length ? <ol className={`transcript-list ${transcriptDraft ? "is-editing" : ""}`}>
            {shownTranscript.segments.map((segment, index) => <li key={segment.id ?? segment.sequence}>
              {transcriptDraft ? <>
                <div className="segment-edit-grid">
                  <label>{t("detail.speaker")}<input value={segment.speaker_label} onChange={(event) => setTranscriptDraft((current) => current ? { ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, speaker_label: event.target.value } : item) } : current)} /></label>
                  <label>{t("detail.startTime")}<input min="0" step="0.001" type="number" value={segment.start_time} onChange={(event) => setTranscriptDraft((current) => current ? { ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, start_time: Number(event.target.value) } : item) } : current)} /></label>
                  <label>{t("detail.endTime")}<input min="0" step="0.001" type="number" value={segment.end_time} onChange={(event) => setTranscriptDraft((current) => current ? { ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, end_time: Number(event.target.value) } : item) } : current)} /></label>
                </div>
                <textarea value={segment.text} onChange={(event) => setTranscriptDraft((current) => current ? { ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, text: event.target.value } : item) } : current)} />
              </> : <><div className="segment-meta"><time>{formatTimestamp(segment.start_time)}</time><strong>{segment.speaker_label}</strong>{segment.language ? <span>{segment.language.toUpperCase()}</span> : null}{segment.has_overlap ? <span className="overlap-label">{t("detail.overlap")}</span> : null}</div><p>{segment.text}</p></>}
            </li>)}
          </ol> : null}
        </section>
      ) : null}

      {tab === "analysis" ? (
        <section aria-labelledby="tab-analysis" className="tab-panel analysis-stack" id="panel-analysis" role="tabpanel">
          <div className="panel analysis-toolbar">
            <div><p className="panel-kicker">LM Studio</p><h2>{t("analysis.title")}</h2></div>
            <div className="inline-actions">
              {analysisDraft ? <>
                <button className="button" disabled={mutationPending !== null} type="button" onClick={() => void saveAnalysis()}>{mutationPending === "analysis" ? t("detail.saving") : t("detail.save")}</button>
                <button className="button button-secondary" type="button" onClick={() => setAnalysisDraft(null)}>{t("detail.cancel")}</button>
              </> : <>
                {shownAnalysis ? <button className="button button-secondary" type="button" onClick={() => setAnalysisDraft(cloneResult(shownAnalysis))}>{t("detail.edit")}</button> : null}
                {shownAnalysis ? <button className="button button-secondary" type="button" onClick={() => void copyText("analysis-all", formatAnalysisCopy(shownAnalysis))}>{copied === "analysis-all" ? t("detail.copied") : t("analysis.copyAll")}</button> : null}
                <button className="button" disabled={status.status !== "completed" || mutationPending !== null} type="button" onClick={() => void handleReanalyse()}>{mutationPending === "reanalyse" ? t("analysis.reprocessing") : t("analysis.reprocess")}</button>
              </>}
            </div>
          </div>
          {analysis.kind === "loading" || analysis.kind === "idle" ? <LoadingView label={t("analysis.loading")} /> : null}
          {analysis.kind === "pending" ? <div className="panel empty-state compact-empty"><h3>{t("analysis.pending")}</h3><p>{t("analysis.pendingHelp")}</p></div> : null}
          {analysis.kind === "error" ? <div className="notice notice-error" role="alert">{analysis.message}</div> : null}
          {analysis.kind === "ready" && analysis.data.status === "stale" ? <div className="notice notice-action" role="status">{t("analysis.stale")}</div> : null}
          {analysis.kind === "ready" && analysis.data.status === "skipped" ? <div className="notice notice-action" role="status">{t("analysis.skipped")}</div> : null}
          {analysis.kind === "ready" && analysis.data.status === "failed" ? <div className="notice notice-error" role="status">{analysis.data.error?.message ?? t("analysis.failed")}</div> : null}
          {analysis.kind === "ready" && analysis.data.job?.kind === "analysis" && analysis.data.job.status === "failed" ? <div className="notice notice-error" role="status">{analysis.data.job.error?.message ?? t("analysis.failedPreserved")}</div> : null}
          {analysis.kind === "ready" && analysis.data.job?.kind === "analysis" && ["queued", "processing"].includes(analysis.data.job.status) ? <div className="notice notice-action" role="status">{t("analysis.refreshing")}</div> : null}
          {shownAnalysis ? <AnalysisContent result={shownAnalysis} editing={analysisDraft !== null} copied={copied} playingClip={playingClip} setResult={setAnalysisDraft} copyText={copyText} playClip={playClip} /> : null}
        </section>
      ) : null}
    </section>
  );
}

function AnalysisContent({ result, editing, copied, playingClip, setResult, copyText, playClip }: { result: AnalysisResultV2; editing: boolean; copied: string | null; playingClip: string | null; setResult: (value: AnalysisResultV2 | null) => void; copyText: (key: string, text: string) => Promise<void>; playClip: (key: string, startTime: number, endTime: number | null) => Promise<void> }) {
  const { t } = useI18n();
  const update = (mutate: (draft: AnalysisResultV2) => void) => {
    const next = cloneResult(result);
    mutate(next);
    setResult(next);
  };
  return <>
    <section className="panel analysis-section">
      <div className="panel-heading-row"><div><p className="panel-kicker">Description</p><h3>{t("analysis.description")}</h3></div><button className="text-button" type="button" onClick={() => void copyText("description", `${result.description.ja}\n\n${result.description.zh_hk}`)}>{copied === "description" ? t("detail.copied") : t("analysis.copy")}</button></div>
      <div className="bilingual-grid">
        <div><span>日本語</span>{editing ? <textarea value={result.description.ja} onChange={(event) => update((next) => { next.description.ja = event.target.value; })} /> : <p>{result.description.ja}</p>}</div>
        <div><span>廣東話</span>{editing ? <textarea value={result.description.zh_hk} onChange={(event) => update((next) => { next.description.zh_hk = event.target.value; })} /> : <p>{result.description.zh_hk}</p>}</div>
      </div>
      {result.summary ? <div className="analysis-summary">
        <div className="analysis-subheading"><h4>{t("analysis.summary")}</h4><button className="text-button" type="button" onClick={() => void copyText("summary", `${result.summary?.ja ?? ""}\n\n${result.summary?.zh_hk ?? ""}`)}>{copied === "summary" ? t("detail.copied") : t("analysis.copy")}</button></div>
        <div className="bilingual-grid summary-grid">
          <div><span>日本語</span>{editing ? <textarea value={result.summary.ja} onChange={(event) => update((next) => { if (next.summary) next.summary.ja = event.target.value; })} /> : <p>{result.summary.ja}</p>}</div>
          <div><span>廣東話</span>{editing ? <textarea value={result.summary.zh_hk} onChange={(event) => update((next) => { if (next.summary) next.summary.zh_hk = event.target.value; })} /> : <p>{result.summary.zh_hk}</p>}</div>
        </div>
      </div> : null}
      <div className="tag-list">{result.tags.map((tag, index) => editing ? <span className="tag-edit" key={index}><input aria-label={`${t("analysis.tag")} ${index + 1} 日本語`} value={tag.ja} onChange={(event) => update((next) => { next.tags[index].ja = event.target.value; })} /><input aria-label={`${t("analysis.tag")} ${index + 1} 廣東話`} value={tag.zh_hk} onChange={(event) => update((next) => { next.tags[index].zh_hk = event.target.value; })} /></span> : <span className="tag-chip" key={`${tag.ja}-${tag.zh_hk}`}>{tag.ja}<small>{tag.zh_hk}</small></span>)}</div>
    </section>
    <section className="analysis-section"><div className="section-heading"><div><p className="panel-kicker">Natural expressions</p><h3>{t("analysis.expressions")}</h3></div><span>{result.natural_expressions.length}</span></div><div className="analysis-card-grid">{result.natural_expressions.map((item, index) => { const clipKey = `expression-${index}`; return <article className="panel analysis-card" key={`${item.segment_sequence}-${index}`}><div className="analysis-card-meta"><span>{formatTimestamp(item.start_time)}</span><strong>{item.speaker_label}</strong><div className="analysis-card-actions"><button aria-label={`${playingClip === clipKey ? t("analysis.stop") : t("analysis.play")} ${item.original_ja}`} className={`clip-button ${playingClip === clipKey ? "is-playing" : ""}`} type="button" onClick={() => void playClip(clipKey, item.start_time, item.end_time)}><span aria-hidden="true">{playingClip === clipKey ? "■" : "▶"}</span>{playingClip === clipKey ? t("analysis.stop") : t("analysis.play")}</button><button className="text-button" type="button" onClick={() => void copyText(clipKey, `${item.original_ja}\n${item.translation_zh_hk}\n${item.usage_ja}\n${item.usage_zh_hk}`)}>{copied === clipKey ? t("detail.copied") : t("analysis.copy")}</button></div></div>{editing ? <><textarea value={item.original_ja} onChange={(event) => update((next) => { next.natural_expressions[index].original_ja = event.target.value; })} /><textarea value={item.translation_zh_hk} onChange={(event) => update((next) => { next.natural_expressions[index].translation_zh_hk = event.target.value; })} /><textarea value={item.usage_ja} onChange={(event) => update((next) => { next.natural_expressions[index].usage_ja = event.target.value; })} /><textarea value={item.usage_zh_hk} onChange={(event) => update((next) => { next.natural_expressions[index].usage_zh_hk = event.target.value; })} /></> : <><blockquote>{item.original_ja}</blockquote><p>{item.translation_zh_hk}</p><div className="bilingual-note"><span>{item.usage_ja}</span><span>{item.usage_zh_hk}</span></div></>}</article>; })}</div></section>
    <section className="analysis-section"><div className="section-heading"><div><p className="panel-kicker">Highlights</p><h3>{t("analysis.highlights")}</h3></div><span>{result.highlights.length}</span></div><div className="analysis-card-grid">{result.highlights.map((item, index) => { const clipKey = `highlight-${index}`; return <article className="panel analysis-card highlight-card" key={`${item.segment_sequence}-${index}`}><div className="analysis-card-meta"><span>{formatTimestamp(item.start_time)}</span><strong>{item.speaker_label}</strong><div className="analysis-card-actions"><button aria-label={`${playingClip === clipKey ? t("analysis.stop") : t("analysis.play")} ${item.original_ja}`} className={`clip-button ${playingClip === clipKey ? "is-playing" : ""}`} type="button" onClick={() => void playClip(clipKey, item.start_time, item.end_time)}><span aria-hidden="true">{playingClip === clipKey ? "■" : "▶"}</span>{playingClip === clipKey ? t("analysis.stop") : t("analysis.play")}</button><button className="text-button" type="button" onClick={() => void copyText(clipKey, `${item.original_ja}\n${item.translation_zh_hk}\n${item.reason_ja}\n${item.reason_zh_hk}`)}>{copied === clipKey ? t("detail.copied") : t("analysis.copy")}</button></div></div>{editing ? <><textarea value={item.original_ja} onChange={(event) => update((next) => { next.highlights[index].original_ja = event.target.value; })} /><textarea value={item.translation_zh_hk} onChange={(event) => update((next) => { next.highlights[index].translation_zh_hk = event.target.value; })} /><textarea value={item.reason_ja} onChange={(event) => update((next) => { next.highlights[index].reason_ja = event.target.value; })} /><textarea value={item.reason_zh_hk} onChange={(event) => update((next) => { next.highlights[index].reason_zh_hk = event.target.value; })} /></> : <><blockquote>{item.original_ja}</blockquote><p>{item.translation_zh_hk}</p><div className="bilingual-note"><span>{item.reason_ja}</span><span>{item.reason_zh_hk}</span></div></>}</article>; })}</div></section>
  </>;
}

function formatAnalysisCopy(result: AnalysisResultV2): string {
  const tags = result.tags.map((tag) => `${tag.ja} / ${tag.zh_hk}`).join(", ");
  const expressions = result.natural_expressions.map((item) => `[${formatTimestamp(item.start_time)}] ${item.original_ja}\n${item.translation_zh_hk}\n${item.usage_ja}\n${item.usage_zh_hk}`).join("\n\n");
  const highlights = result.highlights.map((item) => `[${formatTimestamp(item.start_time)}] ${item.original_ja}\n${item.translation_zh_hk}\n${item.reason_ja}\n${item.reason_zh_hk}`).join("\n\n");
  const summary = result.summary ? `${result.summary.ja}\n\n${result.summary.zh_hk}` : "";
  return `${result.description.ja}\n\n${result.description.zh_hk}\n\n${summary}\n\n${tags}\n\n${expressions}\n\n${highlights}`.trim();
}
