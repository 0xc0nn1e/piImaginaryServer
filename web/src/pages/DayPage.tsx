import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, getDay, listDays, reprocessDaySummary } from "../api";
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

  // Chained timeouts, not an interval: a slow poll would otherwise overlap the
  // next one and an older response could land last, overwriting fresher state.
  const running = useRef(false);
  running.current = Boolean(summaryJobActive);

  useEffect(() => {
    if (!day) return;
    const state = { cancelled: false, timer: 0 };
    const load = async () => {
      try {
        const result = await getDay(day);
        if (state.cancelled) return;
        setDetail(result);
        setError(null);
      } catch (caught: unknown) {
        if (state.cancelled || handleUnauthorized(caught)) return;
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
  }, [day, handleUnauthorized, t, summaryJobActive]);

  async function generate() {
    if (!day) return;
    setQueueing(true);
    setNotice(null);
    try {
      await reprocessDaySummary(day);
      setNotice(t("days.queued"));
      setDetail(await getDay(day));
    } catch (caught: unknown) {
      if (handleUnauthorized(caught)) return;
      setNotice(caught instanceof ApiError ? caught.message : t("days.queueError"));
    } finally {
      setQueueing(false);
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
                  {analysed.has(recording.id) ? null : (
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
                <button
                  className="button button-secondary"
                  disabled={queueing || summaryJobActive}
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
