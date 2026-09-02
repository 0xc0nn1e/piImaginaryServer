import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  it("filters the list to one recording day", async () => {
    window.history.replaceState({}, "", "/recordings");
    const days: (string | null)[] = [];
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-27T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        days.push(new URL(path, "http://localhost").searchParams.get("day"));
        return jsonResponse({ items: [recording(0)], limit: PAGE_SIZE, offset: 0 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByDisplayValue("只看未檢查");
    await browser.type(screen.getByLabelText("錄音日期（日本時間）"), "2026-08-27");
    await browser.click(screen.getByRole("button", { name: "套用篩選" }));

    await waitFor(() => expect(days.at(-1)).toBe("2026-08-27"));
    // The chosen day survives in the URL, so the filtered list can be shared
    // and a reload keeps showing it.
    expect(new URL(window.location.href).searchParams.get("day")).toBe("2026-08-27");
  });

  it("does not claim a day the list never filtered by", async () => {
    // A date field accepts a five digit year. Writing the raw field into the
    // URL would leave it and the field showing a filter the request dropped.
    window.history.replaceState({}, "", "/recordings");
    const days: (string | null)[] = [];
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-27T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        days.push(new URL(path, "http://localhost").searchParams.get("day"));
        return jsonResponse({ items: [recording(0)], limit: PAGE_SIZE, offset: 0 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByDisplayValue("只看未檢查");
    const field = screen.getByLabelText("錄音日期（日本時間）");
    fireEvent.change(field, { target: { value: "20260-08-27" } });
    await browser.click(screen.getByRole("button", { name: "套用篩選" }));

    await waitFor(() => expect(new URL(window.location.href).search).not.toContain("day="));
    expect(field).toHaveValue("");
    expect(days.every((value) => value === null)).toBe(true);
  });

  it.each(["2026-02-30", "0000-01-01"])(
    "ignores %s, a day the server cannot hold",
    async (unusable) => {
    // A hand-edited link can carry either. 2026-02-30 rolls over into March
    // rather than failing, and year 0000 parses cleanly here while the
    // server's calendar starts at year 1, so both reach it as a rejection.
    window.history.replaceState({}, "", `/recordings?day=${unusable}&checked=all`);
    const days: (string | null)[] = [];
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-27T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        days.push(new URL(path, "http://localhost").searchParams.get("day"));
        return jsonResponse({ items: [recording(0)], limit: PAGE_SIZE, offset: 0 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => expect(days.length).toBeGreaterThan(0));
    expect(days.every((value) => value === null)).toBe(true);
    // The address bar has to agree with the list: a day nothing filtered by
    // is dropped, while the filters that are in effect stay put.
    await waitFor(() =>
      expect(new URL(window.location.href).searchParams.get("day")).toBeNull(),
    );
    expect(new URL(window.location.href).searchParams.get("checked")).toBe("all");
    },
  );

  it("opens on the recordings that still need review", async () => {
    window.history.replaceState({}, "", "/recordings");
    const queries: string[] = [];
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-27T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        queries.push(new URL(path, "http://localhost").searchParams.get("checked") ?? "");
        return jsonResponse({ items: [recording(0)], limit: PAGE_SIZE, offset: 0 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    // No parameter in the URL still means "not checked yet", so the list is a
    // queue of outstanding work rather than everything ever recorded.
    await waitFor(() => expect(queries).toEqual(["false"]));
    expect(await screen.findByDisplayValue("只看未檢查")).toBeInTheDocument();
  });

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

  it("keeps a failed page load visible when a tick succeeds", async () => {
    window.history.replaceState({}, "", "/recordings?checked=all");
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let listFails = false;
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-28T08:00:00Z" });
      }
      if (path.endsWith("/checked") && init?.method === "PUT") {
        return jsonResponse({ ...recording(0), checked: true });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        if (listFails) return jsonResponse({ error: { code: "boom", message: "no" } }, 500);
        return jsonResponse({ items: [recording(0)], limit: PAGE_SIZE, offset: 0 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    await screen.findByRole("checkbox", { name: "已檢查" });

    // The list refresh starts failing while the page is open. Rows already on
    // screen stay, so the tick is still reachable. The review filter is left on
    // "all" so that ticking updates in place instead of refetching -- otherwise
    // the failing reload would put the notice back and hide the bug.
    listFails = true;
    await browser.type(screen.getByRole("textbox", { name: /裝置/ }), "pi-recorder-01");
    await browser.click(screen.getByRole("button", { name: "套用篩選" }));
    expect(await screen.findByText(/未能讀取錄音紀錄/)).toBeInTheDocument();

    // Re-query: the earlier render is gone, and clicking a detached node would
    // flip the box without ever reaching the handler.
    await browser.click(screen.getByRole("checkbox", { name: "已檢查" }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.filter((c) => String(c[0]).endsWith("/checked"))).toHaveLength(1),
    );
    // The tick works, but the list is still broken and has to keep saying so.
    expect(screen.getByText(/未能讀取錄音紀錄/)).toBeInTheDocument();
  });
});
