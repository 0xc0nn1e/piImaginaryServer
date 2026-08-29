import type { JobStatus, RecordingStatus } from "./types";
import type { Locale } from "./i18n";
import { translate } from "./i18n";

// Days are grouped by the Japan-time calendar day the audio was recorded on,
// so a day page shows Japan time and not the viewer's own zone.
export const DAY_TIMEZONE = "Asia/Tokyo";

export function formatDateTime(value: string | null, locale: Locale): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatDayTime(value: string | null, locale: Locale): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    timeStyle: "short",
    timeZone: DAY_TIMEZONE,
  }).format(date);
}

export function formatDuration(seconds: number, locale: Locale): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const rounded = Math.round(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remaining = rounded % 60;
  if (hours > 0) return translate(locale, "duration.hours", { hours, minutes });
  if (minutes > 0) {
    return translate(locale, "duration.minutes", { minutes, seconds: remaining });
  }
  return translate(locale, "duration.seconds", { seconds: remaining });
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

export function formatTimestamp(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;
  return hours > 0
    ? [hours, minutes, remaining].map((part) => String(part).padStart(2, "0")).join(":")
    : [minutes, remaining].map((part) => String(part).padStart(2, "0")).join(":");
}

export function statusTone(status: RecordingStatus | JobStatus): string {
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "processing") return "active";
  return "neutral";
}
