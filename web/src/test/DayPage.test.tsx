import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    active_job_recording_ids: [],
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
  // A function, so a test can let the day change the way a queued job changes it.
  detail: unknown | (() => unknown),
  options: {
    onRequest?: (path: string, init?: RequestInit) => void;
    reprocess?: Response;
    analysis?: Response;
  } = {},
) {
  const body = () => (typeof detail === "function" ? (detail as () => unknown)() : detail);
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
    if (path.endsWith("/analysis/reprocess")) {
      return Promise.resolve(
        options.analysis ??
          jsonResponse(
            { day: "2026-08-27", queued_recording_ids: [evening.id], skipped: 0 },
            202,
          ),
      );
    }
    if (path.startsWith("/api/v1/days/")) {
      // A whole Response, when a test wants the day to fail rather than change.
      const next = body();
      return Promise.resolve(next instanceof Response ? next : jsonResponse(next));
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
  vi.useRealTimers();
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

  it("queues the day's unanalysed recordings in one press", async () => {
    const paths: string[] = [];
    let detail = dayDetail();
    vi.stubGlobal(
      "fetch",
      mockDayApi(() => detail, {
        onRequest: (path, init) => {
          paths.push(`${init?.method ?? "GET"} ${path}`);
          // What the next read of the day sees once the jobs exist.
          if (path.endsWith("/analysis/reprocess")) {
            detail = dayDetail({ active_job_recording_ids: [evening.id] });
          }
        },
      }),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    // Only the evening recording is unanalysed, so only it is offered.
    await browser.click(await screen.findByRole("button", { name: "分析未分析嘅 1 個錄音" }));

    expect(await screen.findByText("已安排分析 1 個錄音。")).toBeInTheDocument();
    expect(paths).toContain("POST /api/v1/days/2026-08-27/analysis/reprocess");
    // The day is re-read, so the queued recording stops offering itself and
    // says what it is waiting for instead.
    await waitFor(() => expect(screen.queryByRole("button", { name: /分析未分析/ })).toBeNull());
    const queued = screen.getByText("evening.m4a").closest("li");
    expect(within(queued as HTMLElement).getByText("處理中")).toBeInTheDocument();
    expect(screen.getByText("當日仲有 1 個錄音處理緊…")).toBeInTheDocument();
  });

  it("says so when the server could take none of them", async () => {
    // A recording that gained a job elsewhere is skipped, not an error, and
    // the notice has to say that rather than claim work was queued.
    vi.stubGlobal(
      "fetch",
      mockDayApi(dayDetail(), {
        analysis: jsonResponse({ day: "2026-08-27", queued_recording_ids: [], skipped: 1 }, 202),
      }),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "分析未分析嘅 1 個錄音" }));

    expect(
      await screen.findByText("冇錄音可以排隊分析。 有 1 個正在處理，今次略過。"),
    ).toBeInTheDocument();
  });

  it("keeps the queued result when the day cannot be read back", async () => {
    // The jobs exist whether or not the reload succeeds, so calling the batch
    // a failure would deny committed work and invite a second, futile press.
    let queued = false;
    vi.stubGlobal(
      "fetch",
      mockDayApi(
        () =>
          queued
            ? jsonResponse({ error: { code: "unavailable", message: "unavailable" } }, 503)
            : dayDetail(),
        {
          onRequest: (path) => {
            if (path.endsWith("/analysis/reprocess")) queued = true;
          },
        },
      ),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "分析未分析嘅 1 個錄音" }));

    expect(await screen.findByText("已安排分析 1 個錄音。")).toBeInTheDocument();
    // The read says so on its own terms, beside the result it could not replace.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "未能載入呢一日，請檢查連線後再試。",
    );
    // The day on screen is the one from before the press, so the recordings it
    // still calls unanalysed are already queued. Offering them again would ask
    // for work the server has committed.
    expect(screen.getByRole("button", { name: "分析未分析嘅 1 個錄音" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新產生總結" })).toBeDisabled();
  });

  it("catches up on its own once the day can be read again", async () => {
    // Nothing prompts the user to retry, so a day left behind by a failed read
    // has to keep asking until it gets the answer its actions are waiting on.
    let queued = false;
    let failures = 0;
    vi.stubGlobal(
      "fetch",
      mockDayApi(
        () => {
          if (!queued) return dayDetail();
          if (failures++ === 0) {
            return jsonResponse({ error: { code: "unavailable", message: "unavailable" } }, 503);
          }
          return dayDetail({ active_job_recording_ids: [evening.id] });
        },
        {
          onRequest: (path) => {
            if (path.endsWith("/analysis/reprocess")) queued = true;
          },
        },
      ),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "分析未分析嘅 1 個錄音" }));

    // No second press. The mark only appears once a later read gets through,
    // so waiting for it is waiting for the page to have caught up by itself.
    const queuedItem = (await screen.findByText("處理中")).closest("li");
    expect(within(queuedItem as HTMLElement).getByText("evening.m4a")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /分析未分析/ })).toBeNull();
    expect(screen.getByText("已安排分析 1 個錄音。")).toBeInTheDocument();
    // The read that failed on the way is behind it now.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("drops a read that was already in flight when the batch was queued", async () => {
    // The poll answer left over from before the press describes a day without
    // the jobs. Applying it would put that day back and offer the work again,
    // and neither the poll's cancel flag nor a dependency change can be relied
    // on to stop it: this press leaves both untouched.
    vi.useFakeTimers();
    const before = dayDetail({ active_job_recording_ids: [morning.id] });
    const after = dayDetail({ active_job_recording_ids: [morning.id, evening.id] });
    let dayReads = 0;
    let releaseStalePoll: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/auth/setup-status") {
          return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
        }
        if (path === "/api/v1/auth/me") {
          return Promise.resolve(jsonResponse({ user, expires_at: "2026-08-28T08:00:00Z" }));
        }
        if (path.endsWith("/analysis/reprocess")) {
          return Promise.resolve(
            jsonResponse(
              { day: "2026-08-27", queued_recording_ids: [evening.id], skipped: 0 },
              202,
            ),
          );
        }
        if (path.startsWith("/api/v1/days/")) {
          dayReads += 1;
          // The second read is the poll the press overtakes: it is held open
          // across the press and answers with the day as it was.
          if (dayReads === 2) {
            return new Promise<Response>((resolve) => {
              releaseStalePoll = () => resolve(jsonResponse(before));
            });
          }
          return Promise.resolve(jsonResponse(dayReads === 1 ? before : after));
        }
        if (path.startsWith("/api/v1/days")) return Promise.resolve(jsonResponse(dayList));
        if (path === "/health/ready") return Promise.resolve(jsonResponse({ status: "ready" }));
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);
    // The morning recording holds a job, so the day is already polling.
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "分析未分析嘅 1 個錄音" })).toBeEnabled(),
    );
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(dayReads).toBe(2));

    fireEvent.click(screen.getByRole("button", { name: "分析未分析嘅 1 個錄音" }));
    await vi.waitFor(() =>
      expect(screen.getByText("已安排分析 1 個錄音。")).toBeInTheDocument(),
    );
    // The held poll answers now, with the day as it was before the press.
    // Only microtasks are flushed: advancing the clock would let the next poll
    // paper over whatever this answer did.
    await act(async () => {
      releaseStalePoll?.();
    });

    // The day stays as the press left it: the evening recording is queued and
    // is not offered a second time.
    expect(screen.queryByRole("button", { name: /分析未分析/ })).toBeNull();
    const queuedItem = screen.getByText("evening.m4a").closest("li");
    expect(within(queuedItem as HTMLElement).getByText("處理中")).toBeInTheDocument();
  });

  it("does not put another day's read on the day now shown", async () => {
    // The route can move while the reload is in flight. Applying that answer
    // would leave the page headed one day and filled with another.
    const yesterday = {
      ...morning,
      id: "33333333-3333-4333-8333-333333333333",
      original_filename: "yesterday.m4a",
      started_at: "2026-08-26T01:00:00Z",
      ended_at: "2026-08-26T01:10:00Z",
    };
    let releaseReload: (() => void) | undefined;
    let reads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/auth/setup-status") {
          return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
        }
        if (path === "/api/v1/auth/me") {
          return Promise.resolve(jsonResponse({ user, expires_at: "2026-08-28T08:00:00Z" }));
        }
        if (path.endsWith("/analysis/reprocess")) {
          return Promise.resolve(
            jsonResponse(
              { day: "2026-08-27", queued_recording_ids: [evening.id], skipped: 0 },
              202,
            ),
          );
        }
        if (path === "/api/v1/days/2026-08-26") {
          return Promise.resolve(
            jsonResponse(
              dayDetail({
                day: "2026-08-26",
                recordings: [yesterday],
                analysed_recording_ids: [yesterday.id],
                summary: null,
                status: null,
              }),
            ),
          );
        }
        if (path === "/api/v1/days/2026-08-27") {
          reads += 1;
          // The reload the press asks for, held open across the move.
          if (reads === 2) {
            // Its payload moves none of the dependencies the poll effect
            // watches, so nothing re-reads afterwards: what this answer puts
            // on screen is what stays there.
            return new Promise<Response>((resolve) => {
              releaseReload = () => resolve(jsonResponse(dayDetail()));
            });
          }
          return Promise.resolve(jsonResponse(dayDetail()));
        }
        if (path.startsWith("/api/v1/days")) return Promise.resolve(jsonResponse(dayList));
        if (path === "/health/ready") return Promise.resolve(jsonResponse({ status: "ready" }));
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "分析未分析嘅 1 個錄音" }));
    await screen.findByText("已安排分析 1 個錄音。");

    await browser.click(screen.getByRole("link", { name: "← 前一日" }));
    expect(await screen.findByText("yesterday.m4a")).toBeInTheDocument();

    // The reload for 27 August answers only now, after the page moved on.
    await act(async () => {
      releaseReload?.();
    });

    expect(window.location.pathname).toBe("/days/2026-08-26");
    expect(screen.getByText("yesterday.m4a")).toBeInTheDocument();
    expect(screen.queryByText("evening.m4a")).toBeNull();
    // The notice belonged to the day that was pressed, not to this one.
    expect(screen.queryByText("已安排分析 1 個錄音。")).toBeNull();
  });

  it("keeps a batch that finished after the page moved off its day", async () => {
    // The request outlives the day it was pressed on. Counting it here would
    // discard the read fetching the day now on screen and leave the pressed
    // day's recordings sitting under the new one.
    const yesterday = {
      ...morning,
      id: "33333333-3333-4333-8333-333333333333",
      original_filename: "yesterday.m4a",
      started_at: "2026-08-26T01:00:00Z",
      ended_at: "2026-08-26T01:10:00Z",
    };
    let releaseBatch: (() => void) | undefined;
    let releaseYesterday: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/auth/setup-status") {
          return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
        }
        if (path === "/api/v1/auth/me") {
          return Promise.resolve(jsonResponse({ user, expires_at: "2026-08-28T08:00:00Z" }));
        }
        if (path.endsWith("/analysis/reprocess")) {
          return new Promise<Response>((resolve) => {
            releaseBatch = () =>
              resolve(
                jsonResponse(
                  { day: "2026-08-27", queued_recording_ids: [evening.id], skipped: 0 },
                  202,
                ),
              );
          });
        }
        if (path === "/api/v1/days/2026-08-26") {
          // Held open, so it is still in flight when the batch answers.
          return new Promise<Response>((resolve) => {
            releaseYesterday = () =>
              resolve(
                jsonResponse(
                  dayDetail({
                    day: "2026-08-26",
                    recordings: [yesterday],
                    analysed_recording_ids: [yesterday.id],
                    summary: null,
                    status: null,
                  }),
                ),
              );
          });
        }
        if (path === "/api/v1/days/2026-08-27") return Promise.resolve(jsonResponse(dayDetail()));
        if (path.startsWith("/api/v1/days")) return Promise.resolve(jsonResponse(dayList));
        if (path === "/health/ready") return Promise.resolve(jsonResponse({ status: "ready" }));
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    document.cookie = "audio_server_csrf=csrf-token; Path=/";
    window.history.replaceState({}, "", "/days/2026-08-27");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "分析未分析嘅 1 個錄音" }));
    await browser.click(screen.getByRole("link", { name: "← 前一日" }));
    await waitFor(() => expect(window.location.pathname).toBe("/days/2026-08-26"));

    // The batch answers first, then the read that was already fetching 26th.
    await act(async () => {
      releaseBatch?.();
    });
    await act(async () => {
      releaseYesterday?.();
    });

    expect(screen.getByText("yesterday.m4a")).toBeInTheDocument();
    expect(screen.queryByText("evening.m4a")).toBeNull();
    expect(screen.queryByText("已安排分析 1 個錄音。")).toBeNull();
  });

  it("offers nothing to queue once every recording is analysed", async () => {
    vi.stubGlobal(
      "fetch",
      mockDayApi(dayDetail({ analysed_recording_ids: [morning.id, evening.id] })),
    );
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    expect(await screen.findByRole("button", { name: "重新產生總結" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /分析未分析/ })).toBeNull();
  });

  it("does not offer a recording that has no transcript yet", async () => {
    // Analysis reads a committed transcript, so the server would only refuse
    // it; offering it would just inflate the count the button promises.
    vi.stubGlobal(
      "fetch",
      mockDayApi(
        dayDetail({ recordings: [morning, { ...evening, processing_status: "processing" }] }),
      ),
    );
    window.history.replaceState({}, "", "/days/2026-08-27");

    render(<App />);

    expect(await screen.findByRole("button", { name: "重新產生總結" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /分析未分析/ })).toBeNull();
  });

  it("redirects the bare route to the newest day", async () => {
    vi.stubGlobal("fetch", mockDayApi(dayDetail()));
    window.history.replaceState({}, "", "/days");

    render(<App />);

    await waitFor(() => expect(window.location.pathname).toBe("/days/2026-08-27"));
  });
});
