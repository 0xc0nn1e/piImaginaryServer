import { afterEach, describe, expect, it, vi } from "vitest";

import { getActivity, getSetupStatus, logout, readCsrfCookie, setupAccount } from "../api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  document.cookie = "audio_server_csrf=; Max-Age=0; Path=/";
});

describe("typed API client", () => {
  it("sends the one-time setup token as a header, never in the JSON body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        user: {
          id: "user-1",
          username: "admin",
          created_at: "2026-08-12T00:00:00Z",
          last_login_at: null,
        },
      }, 201),
    );
    vi.stubGlobal("fetch", fetchMock);

    await setupAccount("setup-secret", "admin", "long-password");

    const [path, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/auth/setup");
    expect(new Headers(options.headers).get("X-Setup-Token")).toBe("setup-secret");
    expect(options.body).toBe(JSON.stringify({ username: "admin", password: "long-password" }));
    expect(options.body).not.toContain("setup-secret");
    expect(options.credentials).toBe("include");
  });

  it("reads the CSRF cookie and mirrors it on logout", async () => {
    document.cookie = "audio_server_csrf=csrf%20value; Path=/; SameSite=Strict";
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "logged_out" }));
    vi.stubGlobal("fetch", fetchMock);

    expect(readCsrfCookie()).toBe("csrf value");
    await logout(readCsrfCookie()!);

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf value");
  });

  it("uses exact setup and activity routes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ setup_required: false, setup_enabled: false }))
      .mockResolvedValueOnce(jsonResponse({ items: [], limit: 100, offset: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    await getSetupStatus();
    await getActivity("recording/with unsafe path");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/auth/setup-status");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/recordings/recording%2Fwith%20unsafe%20path/activity?limit=100&offset=0",
    );
  });
});
