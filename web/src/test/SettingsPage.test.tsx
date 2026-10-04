import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";

const user = {
  id: "user-1",
  username: "admin",
  created_at: "2026-08-12T00:00:00Z",
  last_login_at: "2026-08-12T00:01:00Z",
};

interface Switches {
  auto_analysis: boolean;
  auto_translation: boolean;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockSettingsApi(
  stored: Switches,
  options: {
    read?: () => Promise<Response>;
    write?: (changes: Partial<Switches>, init: RequestInit) => Promise<Response>;
  } = {},
) {
  let current = { ...stored };
  return vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/v1/auth/setup-status") {
      return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
    }
    if (path === "/api/v1/auth/me") {
      return Promise.resolve(jsonResponse({ user, expires_at: "2026-10-04T08:00:00Z" }));
    }
    if (path === "/health/ready") return Promise.resolve(jsonResponse({ status: "ready" }));
    if (path === "/api/v1/queue") {
      return Promise.resolve(jsonResponse({ items: [], processing: 0, queued: 0 }));
    }
    if (path === "/api/v1/settings/processing" && init?.method === "PATCH") {
      const changes = JSON.parse(String(init.body)) as Partial<Switches>;
      if (options.write) return options.write(changes, init);
      current = { ...current, ...changes };
      return Promise.resolve(jsonResponse(current));
    }
    if (path === "/api/v1/settings/processing") {
      return options.read ? options.read() : Promise.resolve(jsonResponse(current));
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function patchCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(
    ([, init]) => (init as RequestInit | undefined)?.method === "PATCH",
  ) as [string, RequestInit][];
}

afterEach(() => {
  window.history.replaceState({}, "", "/");
  document.cookie = "audio_server_csrf=; Max-Age=0; Path=/";
  vi.unstubAllGlobals();
});

describe("settings page", () => {
  it("shows transcription as always on beside the stored LLM switches", async () => {
    vi.stubGlobal("fetch", mockSettingsApi({ auto_analysis: true, auto_translation: false }));
    window.history.replaceState({}, "", "/settings");

    render(<App />);

    const analysis = await screen.findByRole("switch", { name: "分析" });
    const transcription = screen.getByRole("switch", { name: "逐字稿" });
    // Every recording is transcribed, so this one can never be turned off.
    expect(transcription).toBeChecked();
    expect(transcription).toBeDisabled();
    expect(screen.getByText("一直開住")).toBeInTheDocument();
    expect(analysis).toBeChecked();
    expect(analysis).toBeEnabled();
    expect(screen.getByRole("switch", { name: "廣東話譯文" })).not.toBeChecked();
    expect(screen.getByRole("link", { name: /設定/ })).toHaveAttribute("aria-current", "page");
  });

  it("sends only the switch that changed, with the CSRF token", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const fetchMock = mockSettingsApi({ auto_analysis: true, auto_translation: true });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/settings");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("switch", { name: "廣東話譯文" }));

    expect(await screen.findByText("已儲存設定。")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "廣東話譯文" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "分析" })).toBeChecked();
    const [[path, init]] = patchCalls(fetchMock);
    expect(path).toBe("/api/v1/settings/processing");
    // Naming the other switch would let a stale tab turn it back.
    expect(JSON.parse(String(init.body))).toEqual({ auto_translation: false });
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  });

  it("can be switched from the keyboard and keeps focus while saving", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    let release: (() => void) | null = null;
    const fetchMock = mockSettingsApi(
      { auto_analysis: true, auto_translation: true },
      {
        write: (changes) =>
          new Promise<Response>((resolve) => {
            release = () => resolve(jsonResponse({ auto_translation: true, ...changes }));
          }),
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/settings");
    const browser = userEvent.setup();

    render(<App />);
    const analysis = await screen.findByRole("switch", { name: "分析" });
    analysis.focus();
    await browser.keyboard(" ");

    await screen.findByText("儲存緊…");
    expect(analysis).toHaveFocus();
    expect(analysis).toHaveAttribute("aria-disabled", "true");
    // A second press while the first is still saving is not sent.
    await browser.keyboard(" ");
    expect(patchCalls(fetchMock)).toHaveLength(1);

    await act(async () => {
      release?.();
    });
    await waitFor(() => expect(analysis).not.toBeChecked());
    expect(analysis).toHaveFocus();
    expect(analysis).not.toHaveAttribute("aria-disabled");
  });

  it("keeps the stored state and says so when a change is not saved", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    vi.stubGlobal(
      "fetch",
      mockSettingsApi(
        { auto_analysis: true, auto_translation: true },
        {
          write: () =>
            Promise.resolve(
              jsonResponse({ error: { code: "internal_server_error", message: "x" } }, 500),
            ),
        },
      ),
    );
    window.history.replaceState({}, "", "/settings");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("switch", { name: "分析" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("未能儲存設定，請再試一次。");
    expect(screen.getByRole("switch", { name: "分析" })).toBeChecked();
  });

  it("explains a refused security check instead of a generic failure", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    vi.stubGlobal(
      "fetch",
      mockSettingsApi(
        { auto_analysis: true, auto_translation: true },
        {
          write: () =>
            Promise.resolve(
              jsonResponse(
                { error: { code: "csrf_validation_failed", message: "CSRF failed." } },
                403,
              ),
            ),
        },
      ),
    );
    window.history.replaceState({}, "", "/settings");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("switch", { name: "分析" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("安全驗證失敗，請重新登入後再試。");
  });

  it("offers a retry when the switches cannot be read", async () => {
    let reads = 0;
    vi.stubGlobal(
      "fetch",
      mockSettingsApi(
        { auto_analysis: false, auto_translation: true },
        {
          read: () => {
            reads += 1;
            return Promise.resolve(
              reads === 1
                ? jsonResponse({ error: { code: "internal_server_error", message: "x" } }, 500)
                : jsonResponse({ auto_analysis: false, auto_translation: true }),
            );
          },
        },
      ),
    );
    window.history.replaceState({}, "", "/settings");
    const browser = userEvent.setup();

    render(<App />);
    expect(await screen.findByRole("heading", { name: "未能顯示設定" })).toBeInTheDocument();
    await browser.click(screen.getByRole("button", { name: "再試一次" }));

    expect(await screen.findByRole("switch", { name: "分析" })).not.toBeChecked();
  });

  it("returns to the login page when the session has ended", async () => {
    vi.stubGlobal(
      "fetch",
      mockSettingsApi(
        { auto_analysis: true, auto_translation: true },
        {
          read: () =>
            Promise.resolve(
              jsonResponse(
                { error: { code: "authentication_required", message: "Sign in." } },
                401,
              ),
            ),
        },
      ),
    );
    window.history.replaceState({}, "", "/settings");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "登入" })).toBeInTheDocument();
  });

  it("is linked from the queue, where the chain it controls is explained", async () => {
    vi.stubGlobal("fetch", mockSettingsApi({ auto_analysis: false, auto_translation: true }));
    window.history.replaceState({}, "", "/queue");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("link", { name: "自動處理設定" }));

    expect(await screen.findByRole("switch", { name: "分析" })).not.toBeChecked();
    expect(window.location.pathname).toBe("/settings");
  });

  it("follows the interface language, including a notice already on screen", async () => {
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    vi.stubGlobal("fetch", mockSettingsApi({ auto_analysis: true, auto_translation: true }));
    window.history.replaceState({}, "", "/settings");
    const browser = userEvent.setup();

    render(<App />);
    await browser.click(await screen.findByRole("switch", { name: "分析" }));
    await screen.findByText("已儲存設定。");
    await browser.click(screen.getAllByRole("button", { name: "日本語" })[0]);

    expect(await screen.findByRole("switch", { name: "広東語訳" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "分析" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "文字起こし" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "アップロード後の自動処理" })).toBeInTheDocument();
    expect(screen.getByText("設定を保存しました。")).toBeInTheDocument();
    expect(screen.queryByText("已儲存設定。")).not.toBeInTheDocument();
  });
});
