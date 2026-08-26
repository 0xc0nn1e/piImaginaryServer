import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";

const PAGE_SIZE = 20;

const user = {
  id: "user-1",
  username: "admin",
  created_at: "2026-08-12T00:00:00Z",
  last_login_at: "2026-08-12T00:01:00Z",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function recording(index: number) {
  return {
    id: `recording-${index}`,
    device_id: "pi-recorder-01",
    original_filename: `meeting-${index}.wav`,
    mime_type: "audio/wav",
    audio_format: "wav",
    file_size: 1024,
    sha256: "0".repeat(64),
    started_at: "2026-08-25T09:00:00Z",
    ended_at: "2026-08-25T09:15:00Z",
    duration_seconds: 900,
    sample_rate: 16000,
    channels: 1,
    processing_status: "completed" as const,
    checked: false,
    created_at: "2026-08-25T09:15:00Z",
    updated_at: "2026-08-25T09:15:00Z",
  };
}

afterEach(() => {
  vi.useRealTimers();
  window.history.replaceState({}, "", "/");
});

describe("review marks on the recordings list", () => {
  it("keeps a row visible after it stops matching the filter and pulls the next offset back", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    window.history.replaceState({}, "", "/recordings?checked=false");
    const listedOffsets: string[] = [];
    const gate: { release?: () => void } = {};

    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-26T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        listedOffsets.push(new URL(path, "http://localhost").searchParams.get("offset") ?? "0");
        return jsonResponse({
          items: Array.from({ length: PAGE_SIZE }, (_unused, index) => recording(index)),
          limit: PAGE_SIZE,
          offset: 0,
        });
      }
      if (path === "/api/v1/recordings/recording-0/checked") {
        // Hold the response open so the test can act while it is in flight.
        await new Promise<void>((resolve) => {
          gate.release = resolve;
        });
        return jsonResponse({ ...recording(0), checked: true });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    const boxes = await screen.findAllByRole("checkbox", { name: "已檢查" });
    const next = screen.getByRole("button", { name: "下一頁" });
    expect(next).toBeEnabled();

    await browser.click(boxes[0]);

    // While the write is in flight the drift count cannot include this row yet,
    // so advancing a full page would step over the recording it is removing.
    await waitFor(() => expect(next).toBeDisabled());

    gate.release?.();
    await waitFor(() => expect(next).toBeEnabled());

    // The row is still on screen rather than silently vanishing.
    const cards = screen.getAllByRole("article");
    expect(within(cards[0]).getByText(/已唔符合目前篩選條件/)).toBeInTheDocument();

    await browser.click(next);

    // One row left the filtered set, so the next window starts one earlier.
    await waitFor(() => expect(listedOffsets.at(-1)).toBe("19"));
  });

  it("blocks the next page until every concurrent mark has settled", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    window.history.replaceState({}, "", "/recordings?checked=false");
    const release: Record<string, () => void> = {};

    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-26T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        return jsonResponse({
          items: Array.from({ length: PAGE_SIZE }, (_unused, index) => recording(index)),
          limit: PAGE_SIZE,
          offset: 0,
        });
      }
      const match = /^\/api\/v1\/recordings\/(recording-\d+)\/checked$/.exec(path);
      if (match) {
        const id = match[1];
        await new Promise<void>((resolve) => {
          release[id] = resolve;
        });
        return jsonResponse({ ...recording(Number(id.split("-")[1])), checked: true });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    const boxes = await screen.findAllByRole("checkbox", { name: "已檢查" });
    const next = screen.getByRole("button", { name: "下一頁" });

    await browser.click(boxes[0]);
    await browser.click(boxes[1]);
    await waitFor(() => expect(next).toBeDisabled());

    // Wait for the first write to actually land — its row flips to checked —
    // and only then assert that navigation is still blocked, because the second
    // write is outstanding and its row is not counted in the drift yet.
    release["recording-0"]?.();
    await waitFor(() => expect(boxes[0]).toBeChecked());
    expect(boxes[1]).not.toBeChecked();
    expect(next).toBeDisabled();

    release["recording-1"]?.();
    await waitFor(() => expect(next).toBeEnabled());
  });
});
