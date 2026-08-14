import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";

const user = {
  id: "user-1",
  username: "admin",
  created_at: "2026-08-12T00:00:00Z",
  last_login_at: "2026-08-12T00:01:00Z",
};

const savedExpression = {
  id: "bookmark-1",
  kind: "expression",
  source_digest: "a".repeat(64),
  original_ja: "一旦こちらで持ち帰ります。",
  translation_zh_hk: "我哋暫時拎返去研究。",
  note_ja: "職場で保留するときの表現です。",
  note_zh_hk: "職場表示要內部研究時使用。",
  speaker_label: "SPEAKER_00",
  start_time: 5,
  end_time: 7,
  recording_id: "d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a",
  source_label: "meeting.flac",
  source_deleted_at: null,
  created_at: "2026-08-13T00:00:00Z",
};

const orphanedHighlight = {
  id: "bookmark-2",
  kind: "highlight",
  source_digest: "b".repeat(64),
  original_ja: "来週までに結論を出しましょう。",
  translation_zh_hk: "下星期之前要有結論。",
  note_ja: "締め切りを共有する重要な場面です。",
  note_zh_hk: "呢度講明咗死線，好重要。",
  speaker_label: "SPEAKER_01",
  start_time: 42,
  end_time: null,
  recording_id: null,
  source_label: "old-standup.wav",
  source_deleted_at: "2026-08-13T09:00:00Z",
  created_at: "2026-08-12T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockBookmarkApi(items: unknown[], onRequest?: (path: string, init?: RequestInit) => void) {
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    onRequest?.(path, init);
    if (path === "/api/v1/auth/setup-status") {
      return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
    }
    if (path === "/api/v1/auth/me") {
      return Promise.resolve(jsonResponse({ user, expires_at: "2026-08-14T08:00:00Z" }));
    }
    if (path.startsWith("/api/v1/bookmarks/") && init?.method === "DELETE") {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (path.startsWith("/api/v1/bookmarks")) {
      return Promise.resolve(jsonResponse({ items }));
    }
    if (path === "/health/ready") {
      return Promise.resolve(jsonResponse({ status: "ready" }));
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

afterEach(() => {
  window.history.replaceState({}, "", "/");
  document.cookie = "audio_server_csrf=; Max-Age=0; Path=/";
  vi.unstubAllGlobals();
});

describe("bookmarks page", () => {
  it("lists saved quotes and filters them by kind", async () => {
    vi.stubGlobal("fetch", mockBookmarkApi([savedExpression, orphanedHighlight]));
    window.history.replaceState({}, "", "/bookmarks");

    render(<App />);

    expect(await screen.findByText("一旦こちらで持ち帰ります。")).toBeInTheDocument();
    expect(screen.getByText("来週までに結論を出しましょう。")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "重點" }));

    expect(screen.getByText("来週までに結論を出しましょう。")).toBeInTheDocument();
    expect(screen.queryByText("一旦こちらで持ち帰ります。")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "自然日語表達" }));

    expect(screen.getByText("一旦こちらで持ち帰ります。")).toBeInTheDocument();
    expect(screen.queryByText("来週までに結論を出しましょう。")).not.toBeInTheDocument();
  });

  it("keeps a saved quote readable after its recording is deleted", async () => {
    vi.stubGlobal("fetch", mockBookmarkApi([orphanedHighlight]));
    window.history.replaceState({}, "", "/bookmarks");

    render(<App />);

    const card = (await screen.findByText("来週までに結論を出しましょう。")).closest("article");
    expect(card).not.toBeNull();
    const scope = within(card as HTMLElement);
    expect(scope.getByText("原本錄音已刪除")).toBeInTheDocument();
    // Provenance survives even though the recording link cannot.
    expect(scope.getByText("old-standup.wav")).toBeInTheDocument();
    expect(scope.queryByRole("link", { name: "開啟原本錄音" })).not.toBeInTheDocument();
  });

  it("links a saved quote back to its recording while it still exists", async () => {
    vi.stubGlobal("fetch", mockBookmarkApi([savedExpression]));
    window.history.replaceState({}, "", "/bookmarks");

    render(<App />);

    const link = await screen.findByRole("link", { name: "開啟原本錄音" });
    expect(link).toHaveAttribute("href", `/recordings/${savedExpression.recording_id}`);
  });

  it("removes a saved quote and drops it from the list", async () => {
    const requests: { path: string; method?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      mockBookmarkApi([savedExpression], (path, init) =>
        requests.push({ path, method: init?.method }),
      ),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/bookmarks");

    render(<App />);
    await screen.findByText("一旦こちらで持ち帰ります。");

    await userEvent.click(screen.getByRole("button", { name: "移除" }));

    await waitFor(() => {
      expect(screen.queryByText("一旦こちらで持ち帰ります。")).not.toBeInTheDocument();
    });
    expect(
      requests.some(
        (entry) => entry.path === "/api/v1/bookmarks/bookmark-1" && entry.method === "DELETE",
      ),
    ).toBe(true);
    expect(screen.getByText("仲未有收藏。")).toBeInTheDocument();
  });

  it("shows an empty state when nothing is saved yet", async () => {
    vi.stubGlobal("fetch", mockBookmarkApi([]));
    window.history.replaceState({}, "", "/bookmarks");

    render(<App />);

    expect(await screen.findByText("仲未有收藏。")).toBeInTheDocument();
    expect(
      screen.getByText("喺分析結果嘅表達或者重點撳「收藏」，就會集中喺呢度。"),
    ).toBeInTheDocument();
  });
});
