import type {
  ActivityResponse,
  ProcessingRequestResponse,
  RecordingListResponse,
  RecordingStatus,
  RecordingStatusResponse,
  RecordingSummary,
  SessionResponse,
  SetupStatusResponse,
  TranscriptResponse,
} from "./types";
import { getStoredLocale, translate } from "./i18n";

interface ApiErrorPayload {
  error?: {
    code?: string;
    message?: string;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  csrfToken?: string;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (options.csrfToken) {
    headers.set("X-CSRF-Token", options.csrfToken);
  }

  const response = await fetch(path, {
    ...options,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "include",
    headers,
  });
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? ((await response.json()) as ApiErrorPayload | T)
    : undefined;

  if (!response.ok) {
    const errorPayload = payload as ApiErrorPayload | undefined;
    throw new ApiError(
      response.status,
      errorPayload?.error?.code ?? "request_failed",
      errorPayload?.error?.message ?? translate(getStoredLocale(), "common.requestFailed"),
    );
  }
  return payload as T;
}

export function getSetupStatus(): Promise<SetupStatusResponse> {
  return request("/api/v1/auth/setup-status");
}

export async function setupAccount(setupToken: string, username: string, password: string) {
  await request<unknown>("/api/v1/auth/setup", {
    method: "POST",
    headers: { "X-Setup-Token": setupToken },
    body: { username, password },
  });
}

export async function login(username: string, password: string) {
  await request<unknown>("/api/v1/auth/login", {
    method: "POST",
    body: { username, password },
  });
}

export function getSession(): Promise<SessionResponse> {
  return request("/api/v1/auth/me");
}

export async function logout(csrfToken: string) {
  await request<unknown>("/api/v1/auth/logout", {
    method: "POST",
    csrfToken,
  });
}

export function readCsrfCookie(): string | null {
  const prefix = "audio_server_csrf=";
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  if (!match) return null;
  try {
    return decodeURIComponent(match.slice(prefix.length));
  } catch {
    return null;
  }
}

export function listRecordings(params: {
  limit: number;
  offset: number;
  deviceId?: string;
  status?: RecordingStatus | "";
}): Promise<RecordingListResponse> {
  const query = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  });
  if (params.deviceId) query.set("device_id", params.deviceId);
  if (params.status) query.set("status", params.status);
  return request(`/api/v1/recordings?${query.toString()}`);
}

export function getRecording(recordingId: string): Promise<RecordingSummary> {
  return request(`/api/v1/recordings/${encodeURIComponent(recordingId)}`);
}

export function getRecordingStatus(recordingId: string): Promise<RecordingStatusResponse> {
  return request(`/api/v1/recordings/${encodeURIComponent(recordingId)}/status`);
}

export function getActivity(recordingId: string): Promise<ActivityResponse> {
  return request(
    `/api/v1/recordings/${encodeURIComponent(recordingId)}/activity?limit=100&offset=0`,
  );
}

export function getTranscript(recordingId: string): Promise<TranscriptResponse> {
  return request(`/api/v1/recordings/${encodeURIComponent(recordingId)}/transcript`);
}

export function reprocessRecording(recordingId: string): Promise<ProcessingRequestResponse> {
  return request(`/api/v1/recordings/${encodeURIComponent(recordingId)}/reprocess`, {
    method: "POST",
    csrfToken: requireCsrfCookie(),
  });
}

export async function deleteRecording(recordingId: string): Promise<void> {
  await request(`/api/v1/recordings/${encodeURIComponent(recordingId)}`, {
    method: "DELETE",
    csrfToken: requireCsrfCookie(),
  });
}

function requireCsrfCookie(): string {
  const token = readCsrfCookie();
  if (!token) throw new Error("CSRF cookie is unavailable");
  return token;
}
