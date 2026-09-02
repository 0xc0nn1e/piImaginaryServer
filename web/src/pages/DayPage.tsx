import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  ApiError,
  getDay,
  listDays,
  reprocessDayAnalyses,
  reprocessDaySummary,
} from "../api";
import { useAuth } from "../auth/AuthContext";
import { Furigana } from "../components/Furigana";
import { LoadingView } from "../components/LoadingView";
import { StatusBadge } from "../components/StatusBadge";
import { formatDayTime, formatDuration } from "../format";
import { useI18n } from "../i18n";
import type { DayDetailResponse, DayListEntry } from "../types";

const DAY_PAGE_SIZE = 60;
const REFRESH_MS = 5000;

export function DayPage() {
  const { day } = useParams();
  const { locale, t } = useI18n();
  const { invalidate } = useAuth();
  const navigate = useNavigate();
  const [days, setDays] = useState<DayListEntry[] | null>(null);
  const [detail, setDetail] = useState<DayDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [queueing, setQueueing] = useState(false);
  const [analysing, setAnalysing] = useState(false);
  // A request of ours changed the day and the page could not read the result
  // back. The work is committed, so the day on screen is known to be behind:
  // the actions stay shut until a read gets through, or the page would offer
  // to queue what it has already queued.
  const [pendingReload, setPendingReload] = useState(false);
  // A read describes the day as it was when the read was issued. Once a
  // request of ours commits, an answer already in flight describes a day that
  // no longer exists, and applying it would put that day back and reopen the
  // actions the change was queued from. Reads carry the count they were issued
  // under and are dropped once it has moved on. The poll's own cancel flag
  // cannot stand in for this: it is set when the effect re-runs, which a stale
  // answer can beat, and a change that leaves every dependency equal re-runs
  // nothing at all.
  const changes = useRef(0);
  // The day a read was issued for is the other half of that: the route can
  // move while a read is in flight, and its answer describes the day it asked
  // for, not the one now on screen. Applying it would leave the page headed
  // one day and filled with another.
  const shownDay = useRef(day);
  shownDay.current = day;

  const handleUnauthorized = useCallback(
    (caught: unknown) => {
      if (caught instanceof ApiError && caught.status === 401) {
        invalidate();
        navigate("/login", { replace: true });
        return true;
      }
      return false;
    },
    [invalidate, navigate],
  );

  // Re-reading the day after a request that already succeeded. It reports a
  // failure the way the poll does, as a day that could not be read: turning it
  // into the request's own failure would deny work the server has committed
  // and invite the user to ask for it a second time.
  const refresh = useCallback(async () => {
    if (!day) return;
    const issuedAt = changes.current;
    try {
      const result = await getDay(day);
      if (day !== shownDay.current || issuedAt !== changes.current) return;
      setDetail(result);
      setError(null);
      setPendingReload(false);
    } catch (caught: unknown) {
      if (handleUnauthorized(caught)) return;
      if (day !== shownDay.current || issuedAt !== changes.current) return;
      setError(t("days.loadError"));
      setPendingReload(true);
    }
  }, [day, handleUnauthorized, t]);

  useEffect(() => {
    const signal = { cancelled: false };
    void (async () => {
      try {
        const result = await listDays({ limit: DAY_PAGE_SIZE, offset: 0 });
        if (signal.cancelled) return;
        setDays(result.items);
        // The bare route lands on the newest day that actually has recordings.
        if (!day && result.items.length > 0) {
          navigate(`/days/${result.items[0].day}`, { replace: true });
        }
        if (!day && result.items.length === 0) setLoading(false);
      } catch (caught: unknown) {
        if (signal.cancelled || handleUnauthorized(caught)) return;
        setError(t("days.loadError"));
        setLoading(false);
      }
    })();
    return () => {
      signal.cancelled = true;
    };
  }, [day, handleUnauthorized, navigate, t]);

  const summaryJobActive =
    detail?.job?.kind === "daily_summary" &&
    (detail.job.status === "queued" || detail.job.status === "processing");
  // The day's recordings a job already holds. They are what the day is still
  // waiting on, so the page keeps refreshing until the last one lets go.
  const activeJobIds = useMemo(
    () => new Set(detail?.active_job_recording_ids ?? []),
    [detail],
  );
  const recordingJobsActive = activeJobIds.size > 0;

  // Chained timeouts, not an interval: a slow poll would otherwise overlap the
  // next one and an older response could land last, overwriting fresher state.
  const running = useRef(false);
  running.current = Boolean(summaryJobActive) || recordingJobsActive || pendingReload;

  useEffect(() => {
    setNotice(null);
  }, [day]);

  useEffect(() => {
    if (!day) return;
    const state = { cancelled: false, timer: 0 };
    const load = async () => {
      const issuedAt = changes.current;
      try {
        const result = await getDay(day);
        if (state.cancelled || day !== shownDay.current) return;
        if (issuedAt !== changes.current) return;
        setDetail(result);
        setError(null);
        setPendingReload(false);
      } catch (caught: unknown) {
        if (state.cancelled || handleUnauthorized(caught)) return;
        if (day !== shownDay.current || issuedAt !== changes.current) return;
        setError(t("days.loadError"));
      } finally {
        if (!state.cancelled) setLoading(false);
      }
    };
    const tick = async () => {
      await load();
      if (!state.cancelled && running.current) {
        state.timer = window.setTimeout(() => void tick(), REFRESH_MS);
      }
    };
    setLoading(true);
    void tick();
    return () => {
      state.cancelled = true;
      window.clearTimeout(state.timer);
    };
  }, [day, handleUnauthorized, pendingReload, recordingJobsActive, t, summaryJobActive]);

  async function generate() {
    if (!day) return;
    setQueueing(true);
    setNotice(null);
    try {
      await reprocessDaySummary(day);
      // The page can move to another day while the request is in flight. What
      // came back describes the day that was pressed, so none of it belongs
      // here: not the notice, and not the change count, which the reads of the
      // day now on screen are measured against — raising it would discard the
      // read that is fetching this day and leave the last one in its place.
      // The work is queued either way and shows when that day is opened again.
      if (day !== shownDay.current) return;
      changes.current += 1;
      setNotice(t("days.queued"));
      await refresh();
    } catch (caught: unknown) {
      if (handleUnauthorized(caught) || day !== shownDay.current) return;
      setNotice(caught instanceof ApiError ? caught.message : t("days.queueError"));
    } finally {
      setQueueing(false);
    }
  }

  async function analysePending() {
    if (!day) return;
    setAnalysing(true);
    setNotice(null);
    try {
      const result = await reprocessDayAnalyses(day);
      // The page can move to another day while the request is in flight. What
      // came back describes the day that was pressed, so none of it belongs
      // here: not the notice, and not the change count, which the reads of the
      // day now on screen are measured against — raising it would discard the
      // read that is fetching this day and leave the last one in its place.
      // The work is queued either way and shows when that day is opened again.
      if (day !== shownDay.current) return;
      changes.current += 1;
      const queued = result.queued_recording_ids.length;
      // The server decides what it could actually take, so the notice reports
      // its answer rather than the count the button was offering.
      const parts = [
        queued > 0
          ? t("days.analysisQueued", { count: queued })
          : t("days.analysisNothingQueued"),
      ];
      if (result.skipped > 0) parts.push(t("days.analysisSkipped", { count: result.skipped }));
      setNotice(parts.join(" "));
      await refresh();
    } catch (caught: unknown) {
      if (handleUnauthorized(caught) || day !== shownDay.current) return;
      setNotice(caught instanceof ApiError ? caught.message : t("days.analysisError"));
    } finally {
      setAnalysing(false);
    }
  }

  // A summary that never arrived has to say so: the job carries the durable
  // safe message, and without this the queued notice would just disappear.
  const summaryFailed = detail?.job?.status === "failed" || Boolean(detail?.error);
  const failureMessage = detail?.job?.error?.message ?? detail?.error?.message ?? null;

  const index = days && day ? days.findIndex((entry) => entry.day === day) : -1;
  const newer = index > 0 ? days?.[index - 1] : undefined;
  const older = index >= 0 && days ? days[index + 1] : undefined;
  const recordingsById = useMemo(
    () => new Map((detail?.recordings ?? []).map((item) => [item.id, item])),
    [detail],
  );
  const analysed = new Set(detail?.analysed_recording_ids ?? []);
  // The recordings the batch endpoint would pick: transcribed, without an
  // analysis to show, and not already held by a job. Choosing them by the same
  // rule keeps the button from offering work the server would only skip.
  const pendingAnalysis = (detail?.recordings ?? []).filter(
    (recording) =>
      recording.processing_status === "completed" &&
      !analysed.has(recording.id) &&
      !activeJobIds.has(recording.id),
  );

  if (!day && days?.length === 0) {
    return (
      <div className="empty-state">
        <span className="empty-wave" aria-hidden="true" />
        <h2>{t("days.emptyTitle")}</h2>
        <p>{t("days.emptyDescription")}</p>
      </div>
    );
  }

  return (
    <div className="day-page">
      <div className="day-switcher">
        <Link
          aria-disabled={older ? undefined : true}
          className={older ? "button button-secondary" : "button button-secondary is-disabled"}
          to={older ? `/days/${older.day}` : "#"}
        >
          ← {t("days.previousDay")}
        </Link>
        <p className="day-timezone">{t("days.timezoneNote")}</p>
        <Link
          aria-disabled={newer ? undefined : true}
          className={newer ? "button button-secondary" : "button button-secondary is-disabled"}
          to={newer ? `/days/${newer.day}` : "#"}
        >
          {t("days.nextDay")} →
        </Link>
      </div>

      <div className="day-layout">
        <aside aria-label={t("days.recordings")} className="panel day-sidebar">
          <p className="panel-kicker">{t("days.kicker")}</p>
          <h2>{t("days.recordings")}</h2>
          {detail ? (
            <p className="day-counts">
              {t("days.recordingCount", { count: detail.recordings.length })} ·{" "}
              {t("days.analysedCount", { count: detail.analysed_recording_ids.length })}
            </p>
          ) : null}
          <ol className="day-recording-list">
            {(detail?.recordings ?? []).map((recording) => (
              <li key={recording.id}>
                <Link to={`/recordings/${recording.id}`}>
                  <time>{formatDayTime(recording.started_at, locale)}</time>
                  <span className="day-recording-name">{recording.original_filename}</span>
                </Link>
                <div className="day-recording-meta">
                  <StatusBadge status={recording.processing_status} />
                  <span>{formatDuration(recording.duration_seconds, locale)}</span>
                  {activeJobIds.has(recording.id) ? (
                    <span className="day-queued">{t("days.jobActive")}</span>
                  ) : analysed.has(recording.id) ? null : (
                    <span className="day-unanalysed">{t("days.notAnalysed")}</span>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </aside>

        <section aria-label={t("days.title", { day: day ?? "" })} className="panel day-main">
          <div className="panel-heading-row">
            <div>
              <p className="panel-kicker">{t("days.kicker")}</p>
              <h2>{t("days.title", { day: day ?? "" })}</h2>
            </div>
            {detail && detail.recordings.length > 0 ? (
              <div className="inline-actions">
                {pendingAnalysis.length > 0 ? (
                  <button
                    className="button button-secondary"
                    disabled={analysing || pendingReload}
                    type="button"
                    onClick={() => void analysePending()}
                  >
                    {analysing
                      ? t("days.queueing")
                      : t("days.analysePending", { count: pendingAnalysis.length })}
                  </button>
                ) : null}
                <button
                  className="button button-secondary"
                  disabled={queueing || summaryJobActive || pendingReload}
                  type="button"
                  onClick={() => void generate()}
                >
                  {queueing
                    ? t("days.queueing")
                    : detail.summary
                      ? t("days.regenerate")
                      : t("days.generate")}
                </button>
              </div>
            ) : null}
          </div>

          {notice ? (
            <div className="notice notice-action" role="status">
              {notice}
            </div>
          ) : null}
          {error ? (
            <div className="notice notice-error" role="alert">
              {error}
            </div>
          ) : null}
          {summaryJobActive ? (
            <div className="notice notice-action" role="status">
              {t("days.running")}
            </div>
          ) : null}
          {recordingJobsActive ? (
            <div className="notice notice-action" role="status">
              {t("days.analysisRunning", { count: activeJobIds.size })}
            </div>
          ) : null}
          {detail?.stale ? (
            <div className="notice notice-action" role="status">
              {t("days.stale")}
            </div>
          ) : null}
          {summaryFailed ? (
            <div className="notice notice-error" role="alert">
              {t("days.summaryFailed")}
              {failureMessage ? <p className="day-failure-detail">{failureMessage}</p> : null}
            </div>
          ) : null}

          {loading && !detail ? <LoadingView label={t("days.loading")} /> : null}

          {detail && !detail.summary && !loading ? (
            <div className="empty-state compact-empty">
              <h3>{t("days.noSummaryTitle")}</h3>
              <p>{t("days.noSummaryDescription")}</p>
            </div>
          ) : null}

          {detail?.summary ? (
            <div className="day-summary">
              <section className="day-section">
                <h3>{t("days.overview")}</h3>
                <div className="bilingual-note">
                  <span>
                    <Furigana text={detail.summary.overview.ja} readings={detail.furigana} />
                  </span>
                  <span>{detail.summary.overview.zh_hk}</span>
                </div>
              </section>

              {detail.summary.key_points.length > 0 ? (
                <section className="day-section">
                  <h3>{t("days.keyPoints")}</h3>
                  <ol className="day-point-list">
                    {detail.summary.key_points.map((point, position) => {
                      const source = point.recording_id
                        ? recordingsById.get(point.recording_id)
                        : undefined;
                      return (
                        <li key={`${point.recording_id ?? "unlinked"}-${position}`}>
                          <div className="bilingual-note">
                            <span>
                              <Furigana text={point.ja} readings={detail.furigana} />
                            </span>
                            <span>{point.zh_hk}</span>
                          </div>
                          {source ? (
                            <Link className="day-point-source" to={`/recordings/${source.id}`}>
                              {t("days.pointSource", {
                                time: formatDayTime(source.started_at, locale),
                              })}
                            </Link>
                          ) : null}
                        </li>
                      );
                    })}
                  </ol>
                </section>
              ) : null}

              {detail.summary.tags.length > 0 ? (
                <section className="day-section">
                  <h3>{t("days.tags")}</h3>
                  <div className="tag-list">
                    {detail.summary.tags.map((tag) => (
                      <span className="tag-chip" key={`${tag.ja}-${tag.zh_hk}`}>
                        {tag.ja}
                        <small>{tag.zh_hk}</small>
                      </span>
                    ))}
                  </div>
                </section>
              ) : null}
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
