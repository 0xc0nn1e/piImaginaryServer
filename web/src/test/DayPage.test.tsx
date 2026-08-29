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

// 01:00 UTC on 27 August is 10:00 the same day in Tokyo.
const morning = {
  id: "11111111-1111-4111-8111-111111111111",
  device_id: "pi-recorder-01",
  original_filename: "morning.m4a",
  mime_type: "audio/mp4",
  audio_format: "m4a",
  file_size: 2048,
  sha256: "a".repeat(64),
  started_at: "2026-08-27T01:00:00Z",
  ended_at: "2026-08-27T01:12:00Z",
  duration_seconds: 720,
  sample_rate: 16000,
  channels: 1,
  processing_status: "completed",
  checked: false,
  created_at: "2026-08-27T01:20:00Z",
  updated_at: "2026-08-27T01:20:00Z",
};

const evening = {
  ...morning,
  id: "22222222-2222-4222-8222-222222222222",
  original_filename: "evening.m4a",
  started_at: "2026-08-27T11:00:00Z",
  ended_at: "2026-08-27T11:20:00Z",
  duration_seconds: 1200,
};

const dayList = {
  items: [
    {
      day: "2026-08-27",
      recording_count: 2,
      analysed_count: 1,
      summary_status: "completed",
      summary_stale: false,
    },
    {
      day: "2026-08-26",
      recording_count: 1,
      analysed_count: 1,
      summary_status: null,
      summary_stale: false,
    },
  ],
  limit: 60,
  offset: 0,
};

const summary = {
  overview: { ja: "打ち合わせが中心の一日。", zh_hk: "以開會為主嘅一日。" },
  key_points: [
    { recording_id: morning.id, ja: "進捗を共有した。", zh_hk: "分享咗進度。" },
    { recording_id: null, ja: "結論は持ち越し。", zh_hk: "結論留待下次。" },
  ],
  tags: [{ ja: "会議", zh_hk: "會議" }],
};

function dayDetail(overrides: Record<string, unknown> = {}) {
  return {
    day: "2026-08-27",
    recordings: [morning, evening],
    analysed_recording_ids: [morning.id],
    status: "completed",
    provider: "lmstudio",
    model: "local-model",
    schema_version: "1",
    summary,
    stale: false,
    job: null,
    error: null,
    furigana: {},
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockDayApi(
  detail: unknown,
  options: { onRequest?: (path: string, init?: RequestInit) => void; reprocess?: Response } = {},
) {
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    options.onRequest?.(path, init);
    if (path === "/api/v1/auth/setup-status") {
      return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
    }
    if (path === "/api/v1/auth/me") {
      return Promise.resolve(jsonResponse({ user, expires_at: "2026-08-28T08:00:00Z" }));
    }
    if (path.endsWith("/summary/reprocess")) {
      return Promise.resolve(
        options.reprocess ??
          jsonResponse({ day: "2026-08-27", job_id: "job-1", status: "queued" }, 202),
      );
    }
    if (path.startsWith("/api/v1/days/")) {
      return Promise.resolve(jsonResponse(detail));
    }
    if (path.startsWith("/api/v1/days")) {
      return Promise.resolve(jsonResponse(dayList));
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

describe("day summary page", () => {
  it("lists the day's recordings beside its summary", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail()));
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    expect(await screen.findByText("以開會為主嘅一日。")).toBeInTheDocument();
    expect(screen.getByText("打ち合わせが中心の一日。")).toBeInTheDocument();
    const sidebar = screen.getByRole("complementary", { name: "當日錄音" });
    expect(within(sidebar).getByText("morning.m4a")).toBeInTheDocument();
    expect(within(sidebar).getByText("evening.m4a")).toBeInTheDocument();
    // The day is a Japan-time day, so its clock times are Japan time too:
    // the viewer's own Hong Kong zone would render these an hour earlier.
    expect(within(sidebar).getByText("上午10:00")).toBeInTheDocument();
    expect(within(sidebar).getByText("下午8:00")).toBeInTheDocument();
  });

  it("marks a recording the summary could not read", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail()));
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    // The aside renders before the day arrives, so wait for the list itself.
    const unanalysed = (await screen.findByText("evening.m4a")).closest("li");
    expect(unanalysed).not.toBeNull();
    expect(within(unanalysed as HTMLElement).getByText("未分析")).toBeInTheDocument();
    const analysed = screen.getByText("morning.m4a").closest("li");
    expect(within(analysed as HTMLElement).queryByText("未分析")).not.toBeInTheDocument();
  });

  it("links a key point back to the recording it came from", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail()));
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    const linked = (await screen.findByText("分享咗進度。")).closest("li");
    expect(linked).not.toBeNull();
    const link = within(linked as HTMLElement).getByRole("link", { name: "上午10:00 嘅錄音" });
    expect(link).toHaveAttribute("href", `/recordings/${morning.id}`);
    // A point whose recording has gone still reads, it just links nowhere.
    const unlinked = screen.getByText("結論留待下次。").closest("li");
    expect(within(unlinked as HTMLElement).queryByRole("link")).toBeNull();
  });

  it("queues a summary and reloads the day", async () => {
    const paths: string[] = [];
    vi.stubGlobal(
      "fetch",
      mockDayApi(dayDetail(), { onRequest: (path, init) => paths.push(`${init?.method ?? "GET"} ${path}`) }),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "重新產生總結" }));

    expect(await screen.findByText("已安排產生每日總結；完成前會保留舊有內容。")).toBeInTheDocument();
    expect(paths).toContain("POST /api/v1/days/2026-08-27/summary/reprocess");
    // The day is re-read so the queued job shows up without waiting for a poll.
    expect(paths.filter((entry) => entry === "GET /api/v1/days/2026-08-27").length).toBeGreaterThan(1);
  });

  it("keeps the previous summary visible while it is stale", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail({ stale: true })));
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    expect(
      await screen.findByText("當日嘅分析有更新，重新產生之前總結仍然係舊版本。"),
    ).toBeInTheDocument();
    expect(screen.getByText("以開會為主嘅一日。")).toBeInTheDocument();
  });

  it("shows a day that has no summary yet", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail({ summary: null, status: null })));
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    expect(await screen.findByText("呢一日仲未有總結")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "產生每日總結" })).toBeInTheDocument();
  });

  it("renders model text as plain text", async () => {
    const injected = "<img src=x onerror=alert(1)>";
    vi.stubGlobal(
      "fetch",
      mockDayApi(
        dayDetail({
          summary: { ...summary, overview: { ja: injected, zh_hk: injected } },
        }),
      ),
    );
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    expect(await screen.findAllByText(injected)).toHaveLength(2);
    expect(document.querySelector("img")).toBeNull();
  });

  it("says so when the summary job failed", async () => {
    // Otherwise the queued notice just disappears and nothing explains why.
    vi.stubGlobal(
      "fetch",
      mockDayApi(
        dayDetail({
          summary: null,
          status: null,
          job: {
            id: "job-1",
            kind: "daily_summary",
            status: "failed",
            stage: "analyzing",
            attempt_count: 3,
            max_attempts: 3,
            available_at: "2026-08-27T16:00:00Z",
            started_at: "2026-08-27T16:00:00Z",
            finished_at: "2026-08-27T16:05:00Z",
            error: {
              code: "lmstudio_unavailable",
              type: "RetryableProcessingError",
              message: "LM Studio is temporarily unavailable.",
              stage: "analyzing",
              at: "2026-08-27T16:05:00Z",
            },
          },
        }),
      ),
    );
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("未能產生每日總結，之前嗰份總結仍然保留。");
    expect(alert).toHaveTextContent("LM Studio is temporarily unavailable.");
    // The day can be asked for again rather than being stuck.
    expect(screen.getByRole("button", { name: "產生每日總結" })).toBeEnabled();
  });

  it("redirects the bare route to the newest day", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail()));
    window.history.replaceState({}, "", "/days");

    render(<App />);

    await waitFor(() => expect(window.location.pathname).toBe("/days/2026-08-27"));
  });
});
