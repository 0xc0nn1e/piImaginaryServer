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
    if (path.endsWith("/transcript")) return Promise.resolve(transcriptResponse);
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

  it("queues reprocessing with the CSRF cookie", async () => {
    window.history.replaceState({}, "", `/recordings/${recordingId}`);
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const fetchMock = mockDetailApi(
      jsonResponse({ recording_id: recordingId, status: "completed", text: "", segments: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("button", { name: "重新分析" }));

    expect(
      await screen.findByText("已安排重新分析；舊逐字稿會保留到新結果成功完成。"),
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
