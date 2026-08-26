import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  createBookmark,
  deleteBookmark,
  deleteRecording,
  getActivity,
  getAnalysis,
  getReanalysis,
  getRecording,
  getRecordingAudioUrl,
  getRecordingStatus,
  getTranscript,
  listBookmarks,
  readCsrfCookie,
  reprocessRecording,
  updateAnalysis,
  updateTranscript,
  reprocessTranslation,
  updateTranslations,
} from "../api";
import { useAuth } from "../auth/AuthContext";
import { Furigana } from "../components/Furigana";
import { LoadingView } from "../components/LoadingView";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes, formatDateTime, formatDuration, formatTimestamp } from "../format";
import {
  type TranslationKey,
  activityLabelKey,
  stageLabelKey,
  statusLabelKey,
  useI18n,
} from "../i18n";
import type {
  TranscriptTranslation,
  ActivityResponse,
  AnalysisResponse,
  AnalysisResultV2,
  Bookmark,
  FuriganaMap,
  BookmarkKind,
  RecordingStatusResponse,
  RecordingSummary,
  TranscriptResponse,
} from "../types";

type DetailTab = "overview" | "activity" | "transcript" | "translations" | "analysis";
type TranscriptState =
  | { kind: "idle" | "loading" | "pending" }
  | { kind: "ready"; data: TranscriptResponse }
  | { kind: "error"; message: string };
type AnalysisState =
  | { kind: "idle" | "loading" | "pending" }
  | { kind: "ready"; data: AnalysisResponse }
  | { kind: "error"; message: string };
type Mutation =
  | "reprocess"
  | "reanalyse"
  | "translation"
  | "translations"
  | "delete"
  | "transcript"
  | "analysis";

const terminalStatuses = new Set(["completed", "failed"]);
const failureEvents = new Set(["processing_failed"]);

/** Matches the server's saved-quote identity: kind plus exact Japanese text. */
function bookmarkKey(kind: BookmarkKind, originalJa: string): string {
  return `${kind}|${originalJa.trim()}`;
}

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
  const [transcriptRefreshKey, setTranscriptRefreshKey] = useState(0);
  // The revision is captured when editing starts, not when saving. Polling can
  // replace the transcript mid-edit, and reading it late would let this write
  // silently overwrite a translation the editor never saw.
  const [translationDraft, setTranslationDraft] = useState<
    { revision: number; values: Record<string, string> } | null
  >(null);
  const wasTranslating = useRef(false);
  const editing = useRef(false);

  // Analysis and translation both keep running after a recording is otherwise
  // finished, so polling has to stay awake for either of them.
  const sideJobActive =
    (status?.job?.kind === "analysis" || status?.job?.kind === "translation") &&
    (status.job.status === "queued" || status.job.status === "processing");

  const [analysisDraft, setAnalysisDraft] = useState<AnalysisResultV2 | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [mutationPending, setMutationPending] = useState<Mutation | null>(null);
  const [mutationMessage, setMutationMessage] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const clipEndRef = useRef<number | null>(null);
  const [playingClip, setPlayingClip] = useState<string | null>(null);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);
  const [bookmarkPending, setBookmarkPending] = useState<string | null>(null);

  const savedBookmarks = useMemo(() => {
    const byKey = new Map<string, Bookmark>();
    for (const item of bookmarks) {
      if (item.recording_id === id) byKey.set(bookmarkKey(item.kind, item.original_ja), item);
    }
    return byKey;
  }, [bookmarks, id]);

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

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await listBookmarks();
        if (!controller.signal.aborted) setBookmarks(response.items);
      } catch {
        // Saved quotes are supplementary; a failure here must not block the
        // recording view. The toggle simply shows the unsaved state.
      }
    })();
    return () => controller.abort();
  }, []);

  const toggleBookmark = useCallback(
    async (
      kind: BookmarkKind,
      item: {
        original_ja: string;
        translation_zh_hk: string;
        note_ja: string;
        note_zh_hk: string;
        speaker_label: string;
        start_time: number;
        end_time: number | null;
      },
    ) => {
      const key = bookmarkKey(kind, item.original_ja);
      const csrfToken = readCsrfCookie();
      if (!csrfToken) {
        invalidate();
        return;
      }
      setBookmarkPending(key);
      const existing = savedBookmarks.get(key);
      try {
        if (existing) {
          await deleteBookmark(existing.id, csrfToken);
          setBookmarks((current) => current.filter((entry) => entry.id !== existing.id));
        } else {
          const created = await createBookmark({ kind, recording_id: id, ...item }, csrfToken);
          setBookmarks((current) => [created, ...current]);
        }
        setMutationMessage(null);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 401) {
          invalidate();
          navigate("/login", { replace: true });
          return;
        }
        setMutationMessage(t("analysis.bookmarkError"));
      } finally {
        setBookmarkPending(null);
      }
    },
    [id, invalidate, navigate, savedBookmarks, t],
  );

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
    if (!status || (terminalStatuses.has(status.status) && !sideJobActive)) return undefined;
    const interval = window.setInterval(() => {
      void refreshLiveData()
        .then(() => {
          if (tab === "analysis") return loadAnalysis();
          return undefined;
        })
        .catch(handleRequestError);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [handleRequestError, loadAnalysis, refreshLiveData, sideJobActive, status, tab]);

  useEffect(() => {
    editing.current = transcriptDraft !== null || translationDraft !== null;
  }, [transcriptDraft, translationDraft]);

  const translationJobActive =
    status?.job?.kind === "translation" &&
    (status.job.status === "queued" || status.job.status === "processing");

  useEffect(() => {
    if (translationJobActive) {
      wasTranslating.current = true;
      return;
    }
    if (!wasTranslating.current) return;
    // The job just finished, so the transcript now carries new sentences.
    wasTranslating.current = false;
    setTranscriptRefreshKey((current) => current + 1);
  }, [translationJobActive]);

  useEffect(() => {
    if (tab !== "transcript" && tab !== "translations") return;
    // An open editor holds unsaved words. A background refresh would replace the
    // rows they are keyed to and the typing would vanish with no way to notice.
    // A ref, not a dependency: closing the editor must not itself refetch, or a
    // save would be overwritten by the response it just replaced.
    if (editing.current) return;
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
  }, [id, invalidate, navigate, status?.status, t, tab, transcriptRefreshKey]);

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

  async function saveTranslations() {
    if (!translationDraft || !shownTranscript) return;
    const edited = shownTranscript.translations
      .filter(
        (item) =>
          item.start_segment_id !== null &&
          (translationDraft.values[item.id] ?? item.text_zh_hk) !== item.text_zh_hk,
      )
      .map((item) => ({
        start_segment_id: item.start_segment_id as string,
        text_zh_hk: translationDraft.values[item.id] ?? item.text_zh_hk,
      }));
    if (!edited.length) {
      setTranslationDraft(null);
      return;
    }
    setMutationPending("translations");
    setMutationMessage(null);
    try {
      const saved = await updateTranslations(id, translationDraft.revision, edited);
      setTranscript({ kind: "ready", data: saved });
      setTranslationDraft(null);
      setMutationMessage(t("detail.translationsSaved"));
    } catch (caught: unknown) {
      handleMutationError(caught, t("detail.translationsSaveError"));
    } finally {
      setMutationPending(null);
    }
  }

  async function handleRetranslate() {
    setMutationPending("translation");
    setMutationMessage(null);
    try {
      await reprocessTranslation(id);
      await refreshLiveData();
      setMutationMessage(t("detail.retranslateQueued"));
      // The load effect does not watch transcript.kind, so a refresh has to be
      // asked for explicitly or the panel would sit on its loading state.
      setTranscriptRefreshKey((current) => current + 1);
    } catch (caught: unknown) {
      handleMutationError(caught, t("detail.retranslateError"));
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
  // Readings come from the last server response. A local draft can contain text
  // the server has not read yet, and Furigana falls back to plain text for any
  // string missing from the map, so edited lines simply lose their reading
  // until the save round-trips.
  const transcriptReadings = transcript.kind === "ready" ? transcript.data.furigana : undefined;
  // A sentence can span several segments, so its Cantonese line is rendered
  // after the last one it covers.
  const translationsByLastSegment = new Map(
    (shownTranscript?.translations ?? []).map((item) => [item.end_segment_id, item]),
  );
  const shownAnalysisReadings = analysis.kind === "ready" ? analysis.data.furigana : undefined;

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
          ["translations", t("detail.tabTranslations")],
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
                <button className="button button-secondary" disabled={mutationPending !== null} type="button" onClick={() => void handleRetranslate()}>{mutationPending === "translation" ? t("detail.retranslating") : t("detail.retranslate")}</button>
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
              </> : <><div className="segment-meta"><time>{formatTimestamp(segment.start_time)}</time><strong>{segment.speaker_label}</strong>{segment.language ? <span>{segment.language.toUpperCase()}</span> : null}{segment.has_overlap ? <span className="overlap-label">{t("detail.overlap")}</span> : null}</div><p><Furigana text={segment.text} readings={transcriptReadings} /></p>{renderTranslation(translationsByLastSegment.get(segment.id), t)}</>}
            </li>)}
          </ol> : null}
        </section>
      ) : null}

      {tab === "translations" ? (
        <section aria-labelledby="tab-translations" className="tab-panel panel" id="panel-translations" role="tabpanel">
          <div className="panel-heading-row">
            <div><p className="panel-kicker">{t("detail.translationsKicker")}</p><h2>{t("detail.tabTranslations")}</h2></div>
            {shownTranscript?.translations.length ? <div className="inline-actions">
              {translationDraft ? <>
                <button className="button" disabled={mutationPending !== null} type="button" onClick={() => void saveTranslations()}>{mutationPending === "translations" ? t("detail.saving") : t("detail.save")}</button>
                <button className="button button-secondary" type="button" onClick={() => { setTranslationDraft(null); setTranscriptRefreshKey((current) => current + 1); }}>{t("detail.cancel")}</button>
              </> : <button className="button button-secondary" type="button" onClick={() => setTranslationDraft({ revision: shownTranscript.translation_revision, values: {} })}>{t("detail.edit")}</button>}
            </div> : null}
          </div>
          {transcript.kind === "loading" || transcript.kind === "idle" ? <LoadingView label={t("detail.transcriptLoading")} /> : null}
          {transcript.kind === "error" ? <div className="notice notice-error" role="alert">{transcript.message}</div> : null}
          {shownTranscript && shownTranscript.translations.length === 0 && transcript.kind === "ready" ? <div className="empty-state compact-empty"><h3>{t("detail.noTranslations")}</h3><p>{t("detail.noTranslationsDescription")}</p></div> : null}
          {translationDraft && translationJobActive ? <div className="notice notice-action" role="status">{t("detail.translationsHeld")}</div> : null}
          {shownTranscript?.translations.length ? (
            <ol className="translation-list">
              {shownTranscript.translations.map((item) => (
                <li key={item.id}>
                  <p className="translation-source"><Furigana text={spanText(shownTranscript, item) ?? item.source_ja} readings={transcriptReadings} /></p>
                  {item.start_segment_id === null ? <small className="translation-detached">{t("detail.translationDetached")}</small> : null}
                  {translationDraft && item.start_segment_id !== null ? (
                    <textarea
                      aria-label={t("detail.translationField")}
                      rows={2}
                      value={translationDraft.values[item.id] ?? item.text_zh_hk}
                      onChange={(event) =>
                        setTranslationDraft((current) =>
                          current
                            ? {
                                ...current,
                                values: { ...current.values, [item.id]: event.target.value },
                              }
                            : current,
                        )
                      }
                    />
                  ) : (
                    <p className={item.stale ? "segment-translation is-stale" : "segment-translation"}>
                      {item.text_zh_hk}
                      {item.stale ? <small>{t("detail.translationStale")}</small> : null}
                    </p>
                  )}
                  <small className="translation-origin">{t(item.source === "manual" ? "detail.translationManual" : "detail.translationMachine")}</small>
                </li>
              ))}
            </ol>
          ) : null}
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
          {shownAnalysis ? <AnalysisContent result={shownAnalysis} editing={analysisDraft !== null} copied={copied} playingClip={playingClip} setResult={setAnalysisDraft} copyText={copyText} playClip={playClip} readings={shownAnalysisReadings} savedBookmarks={savedBookmarks} bookmarkPending={bookmarkPending} toggleBookmark={toggleBookmark} /> : null}
        </section>
      ) : null}
    </section>
  );
}

function AnalysisContent({ result, editing, copied, playingClip, setResult, copyText, playClip, readings, savedBookmarks, bookmarkPending, toggleBookmark }: { result: AnalysisResultV2; editing: boolean; copied: string | null; playingClip: string | null; setResult: (value: AnalysisResultV2 | null) => void; copyText: (key: string, text: string) => Promise<void>; playClip: (key: string, startTime: number, endTime: number | null) => Promise<void>; readings: FuriganaMap | undefined; savedBookmarks: Map<string, Bookmark>; bookmarkPending: string | null; toggleBookmark: (kind: BookmarkKind, item: { original_ja: string; translation_zh_hk: string; note_ja: string; note_zh_hk: string; speaker_label: string; start_time: number; end_time: number | null }) => Promise<void> }) {
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
        <div><span>日本語</span>{editing ? <textarea value={result.description.ja} onChange={(event) => update((next) => { next.description.ja = event.target.value; })} /> : <p><Furigana text={result.description.ja} readings={readings} /></p>}</div>
        <div><span>廣東話</span>{editing ? <textarea value={result.description.zh_hk} onChange={(event) => update((next) => { next.description.zh_hk = event.target.value; })} /> : <p>{result.description.zh_hk}</p>}</div>
      </div>
      {result.summary ? <div className="analysis-summary">
        <div className="analysis-subheading"><h4>{t("analysis.summary")}</h4><button className="text-button" type="button" onClick={() => void copyText("summary", `${result.summary?.ja ?? ""}\n\n${result.summary?.zh_hk ?? ""}`)}>{copied === "summary" ? t("detail.copied") : t("analysis.copy")}</button></div>
        <div className="bilingual-grid summary-grid">
          <div><span>日本語</span>{editing ? <textarea value={result.summary.ja} onChange={(event) => update((next) => { if (next.summary) next.summary.ja = event.target.value; })} /> : <p><Furigana text={result.summary.ja} readings={readings} /></p>}</div>
          <div><span>廣東話</span>{editing ? <textarea value={result.summary.zh_hk} onChange={(event) => update((next) => { if (next.summary) next.summary.zh_hk = event.target.value; })} /> : <p>{result.summary.zh_hk}</p>}</div>
        </div>
      </div> : null}
      <div className="tag-list">{result.tags.map((tag, index) => editing ? <span className="tag-edit" key={index}><input aria-label={`${t("analysis.tag")} ${index + 1} 日本語`} value={tag.ja} onChange={(event) => update((next) => { next.tags[index].ja = event.target.value; })} /><input aria-label={`${t("analysis.tag")} ${index + 1} 廣東話`} value={tag.zh_hk} onChange={(event) => update((next) => { next.tags[index].zh_hk = event.target.value; })} /></span> : <span className="tag-chip" key={`${tag.ja}-${tag.zh_hk}`}><Furigana text={tag.ja} readings={readings} /><small>{tag.zh_hk}</small></span>)}</div>
    </section>
    <section className="analysis-section"><div className="section-heading"><div><p className="panel-kicker">Natural expressions</p><h3>{t("analysis.expressions")}</h3></div><span>{result.natural_expressions.length}</span></div><div className="analysis-card-grid">{result.natural_expressions.map((item, index) => { const clipKey = `expression-${index}`; const savedKey = bookmarkKey("expression", item.original_ja); const isSaved = savedBookmarks.has(savedKey); return <article className="panel analysis-card" key={`${item.segment_sequence}-${index}`}><div className="analysis-card-meta"><span>{formatTimestamp(item.start_time)}</span><strong>{item.speaker_label}</strong><div className="analysis-card-actions"><button aria-label={`${playingClip === clipKey ? t("analysis.stop") : t("analysis.play")} ${item.original_ja}`} className={`clip-button ${playingClip === clipKey ? "is-playing" : ""}`} type="button" onClick={() => void playClip(clipKey, item.start_time, item.end_time)}><span aria-hidden="true">{playingClip === clipKey ? "■" : "▶"}</span>{playingClip === clipKey ? t("analysis.stop") : t("analysis.play")}</button><button className="text-button" type="button" onClick={() => void copyText(clipKey, `${item.original_ja}\n${item.translation_zh_hk}\n${item.usage_ja}\n${item.usage_zh_hk}`)}>{copied === clipKey ? t("detail.copied") : t("analysis.copy")}</button><button aria-pressed={isSaved} className={`text-button bookmark-button ${isSaved ? "is-saved" : ""}`} disabled={bookmarkPending === savedKey} type="button" onClick={() => void toggleBookmark("expression", { original_ja: item.original_ja, translation_zh_hk: item.translation_zh_hk, note_ja: item.usage_ja, note_zh_hk: item.usage_zh_hk, speaker_label: item.speaker_label, start_time: item.start_time, end_time: item.end_time })}>{isSaved ? t("analysis.bookmarked") : t("analysis.bookmark")}</button></div></div>{editing ? <><textarea value={item.original_ja} onChange={(event) => update((next) => { next.natural_expressions[index].original_ja = event.target.value; })} /><textarea value={item.translation_zh_hk} onChange={(event) => update((next) => { next.natural_expressions[index].translation_zh_hk = event.target.value; })} /><textarea value={item.usage_ja} onChange={(event) => update((next) => { next.natural_expressions[index].usage_ja = event.target.value; })} /><textarea value={item.usage_zh_hk} onChange={(event) => update((next) => { next.natural_expressions[index].usage_zh_hk = event.target.value; })} /></> : <><blockquote><Furigana text={item.original_ja} readings={readings} /></blockquote><p>{item.translation_zh_hk}</p><div className="bilingual-note"><span><Furigana text={item.usage_ja} readings={readings} /></span><span>{item.usage_zh_hk}</span></div></>}</article>; })}</div></section>
    <section className="analysis-section"><div className="section-heading"><div><p className="panel-kicker">Highlights</p><h3>{t("analysis.highlights")}</h3></div><span>{result.highlights.length}</span></div><div className="analysis-card-grid">{result.highlights.map((item, index) => { const clipKey = `highlight-${index}`; const savedKey = bookmarkKey("highlight", item.original_ja); const isSaved = savedBookmarks.has(savedKey); return <article className="panel analysis-card highlight-card" key={`${item.segment_sequence}-${index}`}><div className="analysis-card-meta"><span>{formatTimestamp(item.start_time)}</span><strong>{item.speaker_label}</strong><div className="analysis-card-actions"><button aria-label={`${playingClip === clipKey ? t("analysis.stop") : t("analysis.play")} ${item.original_ja}`} className={`clip-button ${playingClip === clipKey ? "is-playing" : ""}`} type="button" onClick={() => void playClip(clipKey, item.start_time, item.end_time)}><span aria-hidden="true">{playingClip === clipKey ? "■" : "▶"}</span>{playingClip === clipKey ? t("analysis.stop") : t("analysis.play")}</button><button className="text-button" type="button" onClick={() => void copyText(clipKey, `${item.original_ja}\n${item.translation_zh_hk}\n${item.reason_ja}\n${item.reason_zh_hk}`)}>{copied === clipKey ? t("detail.copied") : t("analysis.copy")}</button><button aria-pressed={isSaved} className={`text-button bookmark-button ${isSaved ? "is-saved" : ""}`} disabled={bookmarkPending === savedKey} type="button" onClick={() => void toggleBookmark("highlight", { original_ja: item.original_ja, translation_zh_hk: item.translation_zh_hk, note_ja: item.reason_ja, note_zh_hk: item.reason_zh_hk, speaker_label: item.speaker_label, start_time: item.start_time, end_time: item.end_time })}>{isSaved ? t("analysis.bookmarked") : t("analysis.bookmark")}</button></div></div>{editing ? <><textarea value={item.original_ja} onChange={(event) => update((next) => { next.highlights[index].original_ja = event.target.value; })} /><textarea value={item.translation_zh_hk} onChange={(event) => update((next) => { next.highlights[index].translation_zh_hk = event.target.value; })} /><textarea value={item.reason_ja} onChange={(event) => update((next) => { next.highlights[index].reason_ja = event.target.value; })} /><textarea value={item.reason_zh_hk} onChange={(event) => update((next) => { next.highlights[index].reason_zh_hk = event.target.value; })} /></> : <><blockquote><Furigana text={item.original_ja} readings={readings} /></blockquote><p>{item.translation_zh_hk}</p><div className="bilingual-note"><span><Furigana text={item.reason_ja} readings={readings} /></span><span>{item.reason_zh_hk}</span></div></>}</article>; })}</div></section>
  </>;
}

function formatAnalysisCopy(result: AnalysisResultV2): string {
  const tags = result.tags.map((tag) => `${tag.ja} / ${tag.zh_hk}`).join(", ");
  const expressions = result.natural_expressions.map((item) => `[${formatTimestamp(item.start_time)}] ${item.original_ja}\n${item.translation_zh_hk}\n${item.usage_ja}\n${item.usage_zh_hk}`).join("\n\n");
  const highlights = result.highlights.map((item) => `[${formatTimestamp(item.start_time)}] ${item.original_ja}\n${item.translation_zh_hk}\n${item.reason_ja}\n${item.reason_zh_hk}`).join("\n\n");
  const summary = result.summary ? `${result.summary.ja}\n\n${result.summary.zh_hk}` : "";
  return `${result.description.ja}\n\n${result.description.zh_hk}\n\n${summary}\n\n${tags}\n\n${expressions}\n\n${highlights}`.trim();
}


function renderTranslation(
  translation: TranscriptTranslation | undefined,
  t: (key: TranslationKey) => string,
) {
  if (!translation) return null;
  return (
    <p className={translation.stale ? "segment-translation is-stale" : "segment-translation"}>
      {translation.text_zh_hk}
      {translation.stale ? <small>{t("detail.translationStale")}</small> : null}
    </p>
  );
}


function spanText(
  transcript: TranscriptResponse,
  translation: TranscriptTranslation,
): string | null {
  // A stale row still carries the Japanese it was written for. Editing has to
  // happen against what the transcript says now, or the words on screen are not
  // the words being translated.
  if (translation.start_segment_id === null || translation.end_segment_id === null) return null;
  const start = transcript.segments.findIndex((item) => item.id === translation.start_segment_id);
  const end = transcript.segments.findIndex((item) => item.id === translation.end_segment_id);
  if (start === -1 || end === -1 || end < start) return null;
  return transcript.segments.slice(start, end + 1).map((item) => item.text).join("");
}
