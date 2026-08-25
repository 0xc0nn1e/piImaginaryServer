import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, getQueue } from "../api";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime } from "../format";
import { stageLabelKey, useI18n } from "../i18n";
import type { QueueResponse } from "../types";

const REFRESH_MS = 5000;

export function QueuePage() {
  const { locale, t } = useI18n();
  const { invalidate } = useAuth();
  const navigate = useNavigate();
  const [data, setData] = useState<QueueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (signal: { cancelled: boolean }) => {
      try {
        const result = await getQueue();
        if (!signal.cancelled) {
          setData(result);
          setError(null);
        }
      } catch (caught: unknown) {
        if (signal.cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) {
          invalidate();
          navigate("/login", { replace: true });
          return;
        }
        setError(t("queue.error"));
      } finally {
        if (!signal.cancelled) setLoading(false);
      }
    },
    [invalidate, navigate, t],
  );

  useEffect(() => {
    // Chained timeouts, not an interval: a poll slower than REFRESH_MS would
    // otherwise overlap the next one and an older response could land last,
    // overwriting fresher state.
    const state = { cancelled: false, timer: 0 };
    const tick = async () => {
      await load(state);
      if (!state.cancelled) {
        state.timer = window.setTimeout(() => void tick(), REFRESH_MS);
      }
    };
    void tick();
    return () => {
      state.cancelled = true;
      window.clearTimeout(state.timer);
    };
  }, [load]);

  return (
    <section className="panel" aria-label={t("queue.title")}>
      <div className="panel-heading-row">
        <div>
          <p className="panel-kicker">{t("queue.kicker")}</p>
          <h2>{t("queue.title")}</h2>
        </div>
        {data ? (
          <p className="queue-counts">
            {t("queue.processing")}: {data.processing} · {t("queue.waiting")}: {data.queued}
          </p>
        ) : null}
      </div>
      <p className="queue-help">{t("queue.help")}</p>
      <p className="queue-help">{t("queue.separateWorkers")}</p>
      {error ? (
        <div className="notice notice-error" role="alert">
          {error}
        </div>
      ) : null}
      {loading && !data ? <p>{t("queue.loading")}</p> : null}
      {data
        ? (["full", "analysis"] as const).map((kind) => {
            const items = data.items.filter((entry) => entry.job.kind === kind);
            return (
              <div className="queue-section" key={kind}>
                <h3>{t(kind === "analysis" ? "queue.kind.analysis" : "queue.kind.full")}</h3>
                {items.length === 0 ? (
                  <p className="queue-help">{t("queue.empty")}</p>
                ) : (
                  <ol className="queue-list">
                    {items.map((entry) => {
                      const running = entry.job.status === "processing";
                      // A requeued job keeps started_at from the attempt that
                      // failed, so only a running job may show a start time.
                      const retryAt =
                        !running && Date.parse(entry.job.available_at) > Date.now()
                          ? entry.job.available_at
                          : null;
                      return (
                        <li key={entry.job.id} data-status={entry.job.status}>
                          <div>
                            <Link to={`/recordings/${entry.recording_id}`}>
                              {entry.original_filename}
                            </Link>
                            <small>
                              {t(stageLabelKey(entry.job.stage))}
                              {entry.job.attempt_count > 1
                                ? ` · ${t("queue.attempt")} ${entry.job.attempt_count}/${entry.job.max_attempts}`
                                : ""}
                            </small>
                          </div>
                          <small className="queue-time">
                            {running
                              ? `${t("queue.startedAt")} ${formatDateTime(entry.job.started_at, locale)}`
                              : retryAt
                                ? `${t("queue.retryAt")} ${formatDateTime(retryAt, locale)}`
                                : t("queue.ready")}
                          </small>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </div>
            );
          })
        : null}
    </section>
  );
}
