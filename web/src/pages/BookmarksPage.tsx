import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, deleteBookmark, listBookmarks, readCsrfCookie } from "../api";
import { useAuth } from "../auth/AuthContext";
import { LoadingView } from "../components/LoadingView";
import { formatDateTime, formatTimestamp } from "../format";
import { useI18n } from "../i18n";
import type { Bookmark, BookmarkKind } from "../types";

type Filter = "all" | BookmarkKind;

const FILTERS: { value: Filter; labelKey: "bookmarks.all" | "bookmarks.expressions" | "bookmarks.highlights" }[] = [
  { value: "all", labelKey: "bookmarks.all" },
  { value: "expression", labelKey: "bookmarks.expressions" },
  { value: "highlight", labelKey: "bookmarks.highlights" },
];

export function BookmarksPage() {
  const { locale, t } = useI18n();
  const { invalidate } = useAuth();
  const [items, setItems] = useState<Bookmark[] | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const response = await listBookmarks();
        if (signal.aborted) return;
        setItems(response.items);
        setError(null);
      } catch (cause) {
        if (signal.aborted) return;
        if (cause instanceof ApiError && cause.status === 401) {
          invalidate();
          return;
        }
        setError(t("bookmarks.loadError"));
        setItems([]);
      }
    },
    [invalidate, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const remove = async (bookmark: Bookmark) => {
    const csrfToken = readCsrfCookie();
    if (!csrfToken) {
      invalidate();
      return;
    }
    setRemoving(bookmark.id);
    try {
      await deleteBookmark(bookmark.id, csrfToken);
      setItems((current) => (current ?? []).filter((item) => item.id !== bookmark.id));
      setError(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        invalidate();
        return;
      }
      setError(t("bookmarks.removeError"));
    } finally {
      setRemoving(null);
    }
  };

  if (items === null) return <LoadingView />;

  const visible = filter === "all" ? items : items.filter((item) => item.kind === filter);

  return (
    <section className="page bookmarks-page">
      <header className="page-heading">
        <div>
          <p className="panel-kicker">Bookmarks</p>
          <h2>{t("bookmarks.title")}</h2>
          <p className="page-subtitle">{t("bookmarks.subtitle")}</p>
        </div>
        <span className="count-pill">{t("bookmarks.count", { count: visible.length })}</span>
      </header>

      <div className="filter-row" role="group" aria-label={t("bookmarks.title")}>
        {FILTERS.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={filter === option.value}
            className={`filter-chip ${filter === option.value ? "is-active" : ""}`}
            onClick={() => setFilter(option.value)}
          >
            {t(option.labelKey)}
          </button>
        ))}
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      {visible.length === 0 ? (
        <div className="panel empty-state">
          <p>{t("bookmarks.empty")}</p>
          <p className="empty-hint">{t("bookmarks.emptyHint")}</p>
        </div>
      ) : (
        <div className="analysis-card-grid">
          {visible.map((item) => (
            <article
              className={`panel analysis-card ${item.kind === "highlight" ? "highlight-card" : ""}`}
              key={item.id}
            >
              <div className="analysis-card-meta">
                <span>{formatTimestamp(item.start_time)}</span>
                <strong>{item.speaker_label}</strong>
                <div className="analysis-card-actions">
                  <button
                    className="text-button"
                    type="button"
                    disabled={removing === item.id}
                    onClick={() => void remove(item)}
                  >
                    {t("bookmarks.remove")}
                  </button>
                </div>
              </div>
              <blockquote>{item.original_ja}</blockquote>
              <p>{item.translation_zh_hk}</p>
              <div className="bilingual-note">
                <span>{item.note_ja}</span>
                <span>{item.note_zh_hk}</span>
              </div>
              <footer className="bookmark-source">
                {item.recording_id ? (
                  <Link to={`/recordings/${item.recording_id}`}>
                    {t("bookmarks.openRecording")}
                  </Link>
                ) : (
                  <span className="source-gone">{t("bookmarks.sourceDeleted")}</span>
                )}
                <span className="source-label">{item.source_label}</span>
                <time dateTime={item.created_at}>
                  {t("bookmarks.savedAt")}: {formatDateTime(item.created_at, locale)}
                </time>
              </footer>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
