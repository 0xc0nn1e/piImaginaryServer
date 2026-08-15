import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";

const recordingId = "d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a";
const user = {
  id: "user-1",
  username: "admin",
  created_at: "2026-08-12T00:00:00Z",
  last_login_at: "2026-08-12T00:01:00Z",
};
const recording = {
  id: recordingId,
  device_id: "pi-recorder-01",
  original_filename: "meeting.flac",
  mime_type: "audio/flac",
  audio_format: "flac",
  file_size: 2048,
  sha256: "0".repeat(64),
  started_at: "2026-08-10T00:00:00Z",
  ended_at: "2026-08-10T00:01:00Z",
  duration_seconds: 60,
  sample_rate: 16000,
  channels: 1,
  processing_status: "completed",
  created_at: "2026-08-10T00:01:00Z",
  updated_at: "2026-08-10T00:02:00Z",
};
const completedStatus = {
  recording_id: recordingId,
  status: "completed",
  job: {
    id: "job-1",
    status: "completed",
    stage: "completed",
    attempt_count: 1,
    max_attempts: 3,
    available_at: "2026-08-10T00:01:00Z",
    started_at: "2026-08-10T00:01:01Z",
    finished_at: "2026-08-10T00:02:00Z",
    error: null,
  },
};
const completedAnalysis = {
  recording_id: recordingId,
  status: "completed",
  provider: "lmstudio",
  model: "loaded/model",
  schema_version: "2",
  revision: 2,
  result: {
    description: { ja: "会議の説明です。", zh_hk: "呢段係會議內容。" },
    summary: {
      ja: "会議では来週の対応方針を確認しました。担当者は内容を持ち帰って検討します。",
      zh_hk: "會議確認咗下星期嘅處理方向。負責人會將內容帶返去再研究。",
    },
    tags: [{ ja: "検討", zh_hk: "研究" }],
    natural_expressions: [
      {
        segment_sequence: 0,
        start_time: 5,
        end_time: 7,
        speaker_label: "SPEAKER_00",
        original_ja: "一旦こちらで持ち帰ります。",
        translation_zh_hk: "我哋暫時拎返去研究。",
        usage_ja: "職場で保留するときの表現です。",
        usage_zh_hk: "職場表示要內部研究時使用。",
      },
    ],
    highlights: [],
  },
  job: null,
  error: null,
  furigana: {
    "会議の説明です。": [
      { text: "会議", reading: "かいぎ" },
      { text: "の", reading: null },
      { text: "説明", reading: "せつめい" },
      { text: "です。", reading: null },
    ],
    "一旦こちらで持ち帰ります。": [
      { text: "一旦", reading: "いったん" },
      { text: "こちらで", reading: null },
      { text: "持ち帰", reading: "もちかえ" },
      { text: "ります。", reading: null },
    ],
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockDetailApi(transcriptResponse: Response) {
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/auth/setup-status") {
      return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
    }
    if (path === "/api/v1/auth/me") {
      return Promise.resolve(jsonResponse({ user, expires_at: "2026-08-12T08:00:00Z" }));
    }
    if (path.endsWith("/status")) return Promise.resolve(jsonResponse(completedStatus));
    if (path.includes("/activity?")) {
      return Promise.resolve(
        jsonResponse({
          items: [
            {
              id: "event-1",
              job_id: "job-1",
              event_type: "processing_completed",
              job_status: null,
              stage: null,
              attempt_count: 1,
              max_attempts: 3,
              error_code: null,
              error_type: null,
              message: null,
              retry_scheduled: false,
              next_attempt_at: null,
              occurred_at: "2026-08-10T00:02:00Z",
            },
          ],
          limit: 100,
          offset: 0,
        }),
      );
    }
    if (path.startsWith("/api/v1/bookmarks/") && init?.method === "DELETE") {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (path === "/api/v1/bookmarks" && init?.method === "POST") {
      const payload = JSON.parse(String(init.body)) as Record<string, unknown>;
      return Promise.resolve(
        jsonResponse(
          {
            id: "bookmark-new",
            source_digest: "c".repeat(64),
            source_label: "meeting.flac",
            source_deleted_at: null,
            created_at: "2026-08-14T00:00:00Z",
            ...payload,
          },
          201,
        ),
      );
    }
    if (path.startsWith("/api/v1/bookmarks")) {
      return Promise.resolve(jsonResponse({ items: [] }));
    }
    if (path.endsWith("/transcript")) return Promise.resolve(transcriptResponse);
    if (path.endsWith("/analysis")) return Promise.resolve(jsonResponse(completedAnalysis));
    if (path.endsWith("/reprocess")) {
      return Promise.resolve(
        jsonResponse(
          { recording_id: recordingId, job_id: "job-2", status: "queued" },
          202,
        ),
      );
    }
    if (path === `/api/v1/recordings/${recordingId}` && init?.method === "DELETE") {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (path === `/api/v1/recordings/${recordingId}`) {
      return Promise.resolve(jsonResponse(recording));
    }
    if (path.startsWith("/api/v1/recordings?")) {
      return Promise.resolve(jsonResponse({ items: [], limit: 12, offset: 0 }));
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

afterEach(() => {
  window.history.replaceState({}, "", "/");
  document.cookie = "audio_server_csrf=; Max-Age=0; Path=/";
  vi.unstubAllGlobals();
});

describe("recording transcript states", () => {
  it("shows a clear pending state for a 409 transcript response", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal(
      "fetch",
      mockDetailApi(
        jsonResponse(
          { error: { code: "transcript_not_ready", message: "Transcript is not ready." } },
          409,
        ),
      ),
    );
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));

    expect(await screen.findByRole("heading", { name: "逐字稿仍在準備" })).toBeInTheDocument();
  });

  it("distinguishes a completed transcript with no speech segments", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal(
      "fetch",
      mockDetailApi(
        jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
      ),
    );
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));

    expect(
      await screen.findByRole("heading", { name: "處理已完成，但沒有語音內容" }),
    ).toBeInTheDocument();
  });

  it("renders truthful fallbacks for historical activity without stage or status", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal(
      "fetch",
      mockDetailApi(
        jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
      ),
    );
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "處理記錄" }));

    expect(screen.getByText(/階段不詳 · 狀態不詳/)).toBeInTheDocument();
  });

  it("shows the detailed bilingual summary and plays an expression from its timestamp", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal(
      "fetch",
      mockDetailApi(
        jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
      ),
    );
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "分析" }));

    expect(await screen.findByRole("heading", { name: "內容摘要" })).toBeInTheDocument();
    expect(screen.getByText(/會議確認咗下星期嘅處理方向/)).toBeInTheDocument();

    const audio = document.querySelector("audio");
    expect(audio).not.toBeNull();
    Object.defineProperty(audio, "readyState", { configurable: true, value: 1 });
    Object.defineProperty(audio, "duration", { configurable: true, value: 60 });
    const play = vi.spyOn(audio as HTMLAudioElement, "play").mockResolvedValue();
    vi.spyOn(audio as HTMLAudioElement, "pause").mockImplementation(() => undefined);

    await browser.click(screen.getByRole("button", { name: /播放原音.*一旦こちら/ }));

    await waitFor(() => expect(play).toHaveBeenCalledOnce());
    expect((audio as HTMLAudioElement).currentTime).toBe(5);
    expect(screen.getByRole("button", { name: /停止.*一旦こちら/ })).toBeInTheDocument();
  });

  it("saves an analysis expression as a bookmark and reflects the saved state", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const fetchMock = mockDetailApi(
      jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "分析" }));
    await screen.findByRole("heading", { name: "內容摘要" });

    await browser.click(screen.getByRole("button", { name: "收藏" }));

    expect(await screen.findByRole("button", { name: "已收藏" })).toBeInTheDocument();
    const saved = fetchMock.mock.calls.find(
      ([input, init]) => String(input) === "/api/v1/bookmarks" && init?.method === "POST",
    );
    expect(saved).toBeDefined();
    const body = JSON.parse(String((saved as [unknown, RequestInit])[1].body));
    expect(body).toMatchObject({
      kind: "expression",
      recording_id: recordingId,
      original_ja: "一旦こちらで持ち帰ります。",
      // The card's usage text is stored as the bookmark note.
      note_ja: "職場で保留するときの表現です。",
      note_zh_hk: "職場表示要內部研究時使用。",
      speaker_label: "SPEAKER_00",
      start_time: 5,
      end_time: 7,
    });
    expect((saved as [unknown, RequestInit])[1].headers).toBeDefined();
  });

  it("sets hiragana over the analysis description and quote", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal(
      "fetch",
      mockDetailApi(
        jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
      ),
    );
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "分析" }));
    await screen.findByRole("heading", { name: "內容摘要" });

    const readings = Array.from(document.querySelectorAll("rt")).map((node) => node.textContent);
    // The description is rendered through the same component as the quote, so
    // both contribute readings rather than only the expression card.
    expect(readings).toEqual(
      expect.arrayContaining(["かいぎ", "せつめい", "いったん", "もちかえ"]),
    );
  });

  it("queues reprocessing with the CSRF cookie", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const fetchMock = mockDetailApi(
      jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "重新轉錄" }));

    expect(
      await screen.findByText("已安排重新轉錄；新結果完成前會保留舊有內容。"),
    ).toBeInTheDocument();
    const mutation = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/reprocess"));
    expect(mutation?.[1]?.method).toBe("POST");
    expect((mutation?.[1]?.headers as Headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  });

  it("confirms permanent deletion and returns to the recording list", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const fetchMock = mockDetailApi(
      jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "刪除錄音" }));

    expect(confirm).toHaveBeenCalledOnce();
    await waitFor(() => {
      expect(window.location.pathname).toBe("/recordings");
    });
    const mutation = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input) === `/api/v1/recordings/${recordingId}` && init?.method === "DELETE",
    );
    expect((mutation?.[1]?.headers as Headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  });
});
