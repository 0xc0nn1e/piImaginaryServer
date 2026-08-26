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
  checked: false,
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
    "検討": [{ text: "検討", reading: "けんとう" }],
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
    // Description, tag and quote all render through the same component, so
    // each contributes readings rather than only the expression card.
    expect(readings).toEqual(
      expect.arrayContaining(["かいぎ", "せつめい", "けんとう", "いったん", "もちかえ"]),
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

describe("Cantonese translations under the transcript", () => {
  it("renders a sentence translation after the last segment it covers", async () => {
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 1,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "昨日は",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
        {
          id: "segment-2",
          sequence: 1,
          speaker_label: "SPEAKER_00",
          start_time: 1,
          end_time: 2,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [
        {
          id: "translation-1",
          start_segment_id: "segment-1",
          end_segment_id: "segment-2",
          source_ja: "昨日は行きました。",
          text_zh_hk: "尋日去咗。",
          source: "llm",
          stale: false,
        },
      ],
      translation_revision: 1,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal("fetch", mockDetailApi(jsonResponse(transcript)));
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));

    const line = await screen.findByText("尋日去咗。");
    // The sentence spans two segments, so the translation belongs to the last.
    expect(line.closest("li")).toHaveTextContent("行きました。");
    expect(line.closest("li")).not.toHaveTextContent("昨日は");
    expect(line).not.toHaveClass("is-stale");
  });

  it("marks a translation stale once the transcript has been edited", async () => {
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 2,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [
        {
          id: "translation-1",
          start_segment_id: "segment-1",
          end_segment_id: "segment-1",
          source_ja: "行きました。",
          text_zh_hk: "去咗。",
          source: "llm",
          stale: true,
        },
      ],
      translation_revision: 2,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    vi.stubGlobal("fetch", mockDetailApi(jsonResponse(transcript)));
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));

    const line = await screen.findByText(/去咗。/);
    // Kept rather than deleted, but visibly no longer trusted.
    expect(line).toHaveClass("is-stale");
    expect(line).toHaveTextContent("逐字稿改咗");
  });

  it("reloads the transcript after queueing a retranslation", async () => {
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 1,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [],
      translation_revision: 0,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let transcriptLoads = 0;
    const base = mockDetailApi(jsonResponse(transcript));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/translation/reprocess")) {
        return Promise.resolve(
          jsonResponse({ recording_id: recordingId, job_id: "job-2", status: "queued" }, 202),
        );
      }
      if (path.endsWith("/transcript")) {
        transcriptLoads += 1;
        return Promise.resolve(jsonResponse(transcript));
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));
    await waitFor(() => expect(transcriptLoads).toBe(1));

    await browser.click(screen.getByRole("button", { name: "重新翻譯廣東話" }));

    // The load effect does not watch transcript.kind, so the panel would sit on
    // its loading state unless the refresh is asked for explicitly.
    await waitFor(() => expect(transcriptLoads).toBe(2));
    expect(await screen.findByText(/已安排重新翻譯/)).toBeInTheDocument();
  });

  it("fetches the finished translation once the job leaves the queue", async () => {
    const withTranslation = {
      recording_id: recordingId,
      status: "completed",
      revision: 1,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [
        {
          id: "translation-1",
          start_segment_id: "segment-1",
          end_segment_id: "segment-1",
          source_ja: "行きました。",
          text_zh_hk: "去咗。",
          source: "llm",
          stale: false,
        },
      ],
      translation_revision: 1,
      furigana: {},
    };
    const withoutTranslation = { ...withTranslation, translations: [], translation_revision: 0 };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    let translating = true;
    const base = mockDetailApi(jsonResponse(withoutTranslation));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/status")) {
        return Promise.resolve(
          jsonResponse({
            recording_id: recordingId,
            status: "completed",
            job: {
              id: "job-2",
              kind: "translation",
              status: translating ? "processing" : "completed",
              stage: translating ? "translating" : "completed",
              attempt_count: 1,
              max_attempts: 3,
              available_at: "2026-08-10T00:01:00Z",
              started_at: "2026-08-10T00:01:01Z",
              finished_at: translating ? null : "2026-08-10T00:02:00Z",
              error: null,
            },
          }),
        );
      }
      if (path.endsWith("/transcript")) {
        return Promise.resolve(jsonResponse(translating ? withoutTranslation : withTranslation));
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));
    await screen.findByText("行きました。");
    expect(screen.queryByText("去咗。")).not.toBeInTheDocument();

    // The job finishes in the background; polling has to notice and refetch,
    // otherwise the finished translation never reaches the page.
    translating = false;

    expect(await screen.findByText("去咗。", undefined, { timeout: 8000 })).toBeInTheDocument();
  }, 12000);
});

describe("editing a Cantonese translation by hand", () => {
  it("sends only the edited sentence and shows it as manual afterwards", async () => {
    const machine = {
      id: "translation-1",
      start_segment_id: "segment-1",
      end_segment_id: "segment-1",
      source_ja: "行きました。",
      text_zh_hk: "機器譯文",
      source: "llm",
      stale: true,
    };
    const untouched = {
      id: "translation-2",
      start_segment_id: "segment-2",
      end_segment_id: "segment-2",
      source_ja: "はい。",
      text_zh_hk: "係。",
      source: "llm",
      stale: true,
    };
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 1,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [machine, untouched],
      translation_revision: 3,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let sent: unknown = null;
    const base = mockDetailApi(jsonResponse(transcript));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/translations") && init?.method === "PUT") {
        sent = JSON.parse(String(init.body));
        return Promise.resolve(
          jsonResponse({
            ...transcript,
            translation_revision: 4,
            translations: [
              { ...machine, text_zh_hk: "人手譯文", source: "manual", stale: false },
              untouched,
            ],
          }),
        );
      }
      if (path.endsWith("/transcript")) return Promise.resolve(jsonResponse(transcript));
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "廣東話譯文" }));
    await screen.findByText("機器譯文");
    await browser.click(screen.getByRole("button", { name: "編輯" }));

    const field = screen.getAllByRole("textbox", { name: "廣東話譯文" })[0];
    await browser.clear(field);
    await browser.type(field, "人手譯文");
    await browser.click(screen.getByRole("button", { name: "儲存" }));

    await waitFor(() => expect(sent).not.toBeNull());
    // The revision is the one that was on screen when editing started, so a
    // translation that arrived mid-edit cannot be overwritten unnoticed.
    expect(sent).toEqual({
      expected_revision: 3,
      translations: [{ start_segment_id: "segment-1", text_zh_hk: "人手譯文" }],
    });
    expect(await screen.findByText("人手譯文")).toBeInTheDocument();
    expect(screen.getAllByText("手動")).toHaveLength(1);
  });
});

describe("guarding a translation edit against concurrent writes", () => {
  it("saves against the revision that was on screen when editing began", async () => {
    const row = {
      id: "translation-1",
      start_segment_id: "segment-1",
      end_segment_id: "segment-1",
      source_ja: "行きました。",
      text_zh_hk: "機器譯文",
      source: "llm",
      stale: false,
    };
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 1,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [row],
      translation_revision: 3,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let revision = 3;
    const captured: { expected_revision?: number }[] = [];
    const base = mockDetailApi(jsonResponse(transcript));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/translations") && init?.method === "PUT") {
        captured.push(JSON.parse(String(init.body)));
        return Promise.resolve(jsonResponse(transcript));
      }
      if (path.endsWith("/transcript")) {
        return Promise.resolve(jsonResponse({ ...transcript, translation_revision: revision }));
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "廣東話譯文" }));
    await screen.findByText("機器譯文");
    await browser.click(screen.getByRole("button", { name: "編輯" }));

    // A translation job lands while the editor is open.
    revision = 9;

    const field = screen.getAllByRole("textbox", { name: "廣東話譯文" })[0];
    await browser.clear(field);
    await browser.type(field, "人手譯文");
    await browser.click(screen.getByRole("button", { name: "儲存" }));

    await waitFor(() => expect(captured).toHaveLength(1));
    // Reading the revision at save time would send 9 and quietly overwrite work
    // the editor never saw. The server must be given the chance to refuse.
    expect(captured[0].expected_revision).toBe(3);
  });
});

describe("protecting an open translation editor", () => {
  it("keeps unsaved words when a reprocess lands mid-edit", async () => {
    const before = {
      id: "translation-1",
      start_segment_id: "segment-1",
      end_segment_id: "segment-1",
      source_ja: "行きました。",
      text_zh_hk: "機器譯文",
      source: "llm",
      stale: false,
    };
    // Reprocessing rebuilds every row, so the ids the draft is keyed to are gone.
    const after = { ...before, id: "translation-9", text_zh_hk: "新機器譯文" };
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 1,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [before],
      translation_revision: 3,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let rebuilt = false;
    const base = mockDetailApi(jsonResponse(transcript));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/transcript")) {
        return Promise.resolve(
          jsonResponse(
            rebuilt
              ? { ...transcript, translations: [after], translation_revision: 4 }
              : transcript,
          ),
        );
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "廣東話譯文" }));
    await screen.findByText("機器譯文");
    await browser.click(screen.getByRole("button", { name: "編輯" }));

    const field = screen.getAllByRole("textbox", { name: "廣東話譯文" })[0];
    await browser.clear(field);
    await browser.type(field, "我打緊嘅字");

    // A job finishes and rewrites every translation row behind the editor.
    rebuilt = true;
    await browser.click(screen.getByRole("tab", { name: "逐字稿" }));
    await browser.click(screen.getByRole("tab", { name: "廣東話譯文" }));

    // The draft is keyed by row id, so a refresh here would blank the field
    // and the typing would be gone with nothing on screen to show it happened.
    expect(screen.getAllByRole("textbox", { name: "廣東話譯文" })[0]).toHaveValue("我打緊嘅字");
  });
});

describe("confirming a stale translation without retyping it", () => {
  it("clears the stale flag when the reviewer opens the field and saves", async () => {
    const stale = {
      id: "translation-1",
      start_segment_id: "segment-1",
      end_segment_id: "segment-1",
      source_ja: "行きました。",
      text_zh_hk: "去咗。",
      source: "manual",
      stale: true,
    };
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 2,
      text: "",
      segments: [
        {
          id: "segment-1",
          sequence: 0,
          speaker_label: "SPEAKER_00",
          start_time: 0,
          end_time: 1,
          text: "行きました。",
          language: "ja",
          confidence: null,
          has_overlap: false,
        },
      ],
      translations: [stale],
      translation_revision: 5,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const captured: { translations?: unknown[] }[] = [];
    const base = mockDetailApi(jsonResponse(transcript));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/translations") && init?.method === "PUT") {
        captured.push(JSON.parse(String(init.body)));
        return Promise.resolve(
          jsonResponse({
            ...transcript,
            translation_revision: 6,
            translations: [{ ...stale, stale: false }],
          }),
        );
      }
      if (path.endsWith("/transcript")) return Promise.resolve(jsonResponse(transcript));
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "廣東話譯文" }));
    await screen.findByText("去咗。");
    await browser.click(screen.getByRole("button", { name: "編輯" }));

    // The reviewer reads the sentence, decides the wording still holds, and
    // ticks it rather than retyping it. Merely tabbing onto the field must not
    // count, so the confirmation has to be an act of its own.
    await browser.click(screen.getByRole("checkbox", { name: "確認呢句譯文" }));
    await browser.click(screen.getByRole("button", { name: "儲存" }));

    // Requiring a text change would leave the row stale forever, and a later
    // reprocess would then fail to recognise it.
    await waitFor(() => expect(captured).toHaveLength(1));
    expect(captured[0].translations).toEqual([
      { start_segment_id: "segment-1", text_zh_hk: "去咗。" },
    ]);
  });

  it("does not count tabbing through the form as review", async () => {
    const rows = [0, 1].map((index) => ({
      id: `translation-${index}`,
      start_segment_id: `segment-${index}`,
      end_segment_id: `segment-${index}`,
      source_ja: "行きました。",
      text_zh_hk: `譯文${index}`,
      source: "llm",
      stale: true,
    }));
    const transcript = {
      recording_id: recordingId,
      status: "completed",
      revision: 2,
      text: "",
      segments: rows.map((row, index) => ({
        id: row.start_segment_id,
        sequence: index,
        speaker_label: "SPEAKER_00",
        start_time: index,
        end_time: index + 1,
        text: "行きました。",
        language: "ja",
        confidence: null,
        has_overlap: false,
      })),
      translations: rows,
      translation_revision: 5,
      furigana: {},
    };
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const captured: unknown[] = [];
    const base = mockDetailApi(jsonResponse(transcript));
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/translations") && init?.method === "PUT") {
        captured.push(JSON.parse(String(init.body)));
        return Promise.resolve(jsonResponse(transcript));
      }
      if (path.endsWith("/transcript")) return Promise.resolve(jsonResponse(transcript));
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("heading", { name: "meeting.flac" });
    await browser.click(screen.getByRole("tab", { name: "廣東話譯文" }));
    await screen.findByText("譯文0");
    await browser.click(screen.getByRole("button", { name: "編輯" }));

    // Keyboard navigation moves focus across every field without reading a word.
    await browser.tab();
    await browser.tab();
    await browser.tab();
    await browser.tab();
    await browser.click(screen.getByRole("button", { name: "儲存" }));

    // Recording that as review would write a false human confirmation and clear
    // the stale flag on sentences nobody looked at.
    expect(captured).toEqual([]);
    expect(await screen.findByText(/請先揀要儲存嘅譯文/)).toBeInTheDocument();
  });
});

describe("marking a recording reviewed from its own page", () => {
  it("saves the tick without going back to the list", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let sent: { checked?: boolean } | null = null;
    const base = mockDetailApi(
      jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
    );
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/checked") && init?.method === "PUT") {
        sent = JSON.parse(String(init.body));
        return Promise.resolve(jsonResponse({ ...recording, checked: true }));
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    const box = await screen.findByRole("checkbox", { name: "已檢查" });
    expect(box).not.toBeChecked();

    await browser.click(box);

    await waitFor(() => expect(sent).toEqual({ checked: true }));
    expect(await screen.findByRole("checkbox", { name: "已檢查" })).toBeChecked();
  });

  it("clears the failure notice once a retry succeeds", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let failNext = true;
    const base = mockDetailApi(
      jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
    );
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/checked") && init?.method === "PUT") {
        if (failNext) {
          failNext = false;
          return Promise.resolve(
            jsonResponse({ error: { code: "boom", message: "nope" } }, 500),
          );
        }
        return Promise.resolve(jsonResponse({ ...recording, checked: true }));
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    const box = await screen.findByRole("checkbox", { name: "已檢查" });

    await browser.click(box);
    expect(await screen.findByText(/未能更新檢查狀態/)).toBeInTheDocument();

    await browser.click(screen.getByRole("checkbox", { name: "已檢查" }));

    // Leaving the notice up would contradict the state the tick now shows.
    await waitFor(() => expect(screen.queryByText(/未能更新檢查狀態/)).not.toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: "已檢查" })).toBeChecked();
  });
});
