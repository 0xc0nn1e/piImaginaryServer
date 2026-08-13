import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { safeRedirectPath } from "../pages/LoginPage";

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

afterEach(() => {
  window.history.replaceState({}, "", "/");
});

describe("application routes", () => {
  it("uses Japanese by default and switches to Hong Kong Traditional Chinese", async () => {
    window.localStorage.removeItem("wave-archive-locale");
    window.history.replaceState({}, "", "/login");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        if (String(input) === "/api/v1/auth/setup-status") {
          return Promise.resolve(jsonResponse({ setup_required: false, setup_enabled: false }));
        }
        if (String(input) === "/api/v1/auth/me") {
          return Promise.resolve(
            jsonResponse({ error: { code: "unauthorized", message: "Unauthorized" } }, 401),
          );
        }
        throw new Error(`Unexpected request: ${String(input)}`);
      }),
    );
    const browser = userEvent.setup();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "ログイン" })).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("lang", "ja");
    await browser.click(screen.getByRole("button", { name: "繁體中文" }));
    expect(await screen.findByRole("heading", { name: "登入" })).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("lang", "zh-HK");
    expect(window.localStorage.getItem("wave-archive-locale")).toBe("zh-HK");
  });

  it("accepts only same-origin relative post-login paths", () => {
    expect(safeRedirectPath("/recordings/example")).toBe("/recordings/example");
    expect(safeRedirectPath("//attacker.example/path")).toBe("/recordings");
    expect(safeRedirectPath("https://attacker.example/path")).toBe("/recordings");
  });
  it("redirects a new installation to setup and submits the setup token as a header", async () => {
    window.history.replaceState({}, "", "/setup");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ setup_required: true, setup_enabled: true }))
      .mockResolvedValueOnce(jsonResponse({ user }, 201));
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    render(<App />);
    expect(await screen.findByRole("heading", { name: "管理員設定" })).toBeInTheDocument();

    await browser.type(screen.getByLabelText(/^設定憑證/), "one-time-token");
    await browser.type(screen.getByLabelText("使用者名稱"), "admin");
    await browser.type(screen.getByLabelText(/^密碼/), "long-password-123");
    await browser.type(screen.getByLabelText("再次輸入密碼"), "long-password-123");
    await browser.click(screen.getByRole("button", { name: "建立管理員帳戶" }));

    expect(await screen.findByRole("heading", { name: "登入" })).toBeInTheDocument();
    const [, options] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(new Headers(options.headers).get("X-Setup-Token")).toBe("one-time-token");
    expect(options.body).not.toContain("one-time-token");
  });

  it("renders the recording list using safe React text nodes", async () => {
    window.history.replaceState({}, "", "/recordings");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ setup_required: false, setup_enabled: false }))
      .mockResolvedValueOnce(jsonResponse({ user, expires_at: "2026-08-12T08:00:00Z" }))
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              id: "d7fd10c1-e9c8-4ec0-a1ea-1917fa95832a",
              device_id: "pi-recorder-01",
              original_filename: "<img src=x onerror=alert(1)>.flac",
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
            },
          ],
          limit: 20,
          offset: 0,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<App />);

    expect(await screen.findByText("<img src=x onerror=alert(1)>.flac")).toBeInTheDocument();
    expect(container.querySelector("img")).not.toBeInTheDocument();
    expect(container.querySelector(".status-badge")).toHaveTextContent("已完成");
  });

  it("uploads multiple audio files strictly in order and continues after one failure", async () => {
    window.localStorage.setItem("wave-archive-locale", "zh-HK");
    window.history.replaceState({}, "", "/recordings");
    document.cookie = "audio_server_csrf=synthetic-csrf; Path=/; SameSite=Strict";
    const uploadOrder: string[] = [];
    let activeUploads = 0;
    let maxActiveUploads = 0;
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/auth/setup-status") {
        return jsonResponse({ setup_required: false, setup_enabled: false });
      }
      if (path === "/api/v1/auth/me") {
        return jsonResponse({ user, expires_at: "2026-08-12T08:00:00Z" });
      }
      if (path.startsWith("/api/v1/recordings?")) {
        return jsonResponse({ items: [], limit: 20, offset: 0 });
      }
      if (path === "/api/v1/web/recordings") {
        activeUploads += 1;
        maxActiveUploads = Math.max(maxActiveUploads, activeUploads);
        const file = (init?.body as FormData).get("audio") as File;
        uploadOrder.push(file.name);
        await Promise.resolve();
        activeUploads -= 1;
        if (file.name === "first.wav") {
          return jsonResponse({ error: { code: "invalid", message: "bad file" } }, 415);
        }
        return jsonResponse({ recording_id: "recording-2", status: "queued", duplicate: false }, 201);
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const browser = userEvent.setup();

    const { container } = render(<App />);
    const uploadButton = await screen.findByRole("button", { name: "上載音訊" });
    expect(uploadButton).toHaveClass("button-primary");
    await browser.click(uploadButton);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await browser.upload(input, [
      new File(["one"], "first.wav", { type: "audio/wav", lastModified: 1 }),
      new File(["two"], "second.mp3", { type: "audio/mpeg", lastModified: 2 }),
    ]);
    await browser.click(screen.getByRole("button", { name: "開始上載" }));

    await waitFor(() => expect(uploadOrder).toEqual(["first.wav", "second.mp3"]));
    expect(maxActiveUploads).toBe(1);
    expect(await screen.findByText("上載失敗；系統會繼續處理下一個檔案。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "開啟結果" })).toHaveAttribute(
      "href",
      "/recordings/recording-2",
    );
  });
});
