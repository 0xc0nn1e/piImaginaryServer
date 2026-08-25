export type RecordingStatus = "uploaded" | "queued" | "processing" | "completed" | "failed";

export type JobStatus = "queued" | "processing" | "completed" | "failed";
export type JobKind = "full" | "analysis";

export type JobStage =
  | "queued"
  | "preprocessing"
  | "transcribing"
  | "diarizing"
  | "merging"
  | "analyzing"
  | "completed";

export interface User {
  id: string;
  username: string;
  created_at: string;
  last_login_at: string | null;
}

export interface SetupStatusResponse {
  setup_required: boolean;
  setup_enabled: boolean;
}

export interface SessionResponse {
  user: User;
  expires_at: string;
}

export interface RecordingSummary {
  id: string;
  device_id: string;
  original_filename: string;
  mime_type: string;
  audio_format: string;
  file_size: number;
  sha256: string;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  sample_rate: number | null;
  channels: number | null;
  processing_status: RecordingStatus;
  created_at: string;
  updated_at: string;
}

export interface RecordingListResponse {
  items: RecordingSummary[];
  limit: number;
  offset: number;
}

export interface JobError {
  code: string;
  type: string | null;
  message: string;
  stage: JobStage | null;
  at: string | null;
}

export interface JobStatusDetails {
  id: string;
  kind: JobKind;
  status: JobStatus;
  stage: JobStage;
  attempt_count: number;
  max_attempts: number;
  available_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: JobError | null;
}

export interface QueueEntry {
  recording_id: string;
  original_filename: string;
  job: JobStatusDetails;
}

export interface QueueResponse {
  items: QueueEntry[];
  processing: number;
  queued: number;
}

export interface RecordingStatusResponse {
  recording_id: string;
  status: RecordingStatus;
  job: JobStatusDetails | null;
}

export interface ProcessingRequestResponse {
  recording_id: string;
  job_id: string;
  status: JobStatus;
}

export interface ActivityItem {
  id: string;
  job_id: string;
  job_kind: JobKind;
  event_type: string;
  job_status: JobStatus | null;
  stage: JobStage | null;
  attempt_count: number;
  max_attempts: number;
  error_code: string | null;
  error_type: string | null;
  message: string | null;
  retry_scheduled: boolean;
  next_attempt_at: string | null;
  occurred_at: string;
}

export interface ActivityResponse {
  items: ActivityItem[];
  limit: number;
  offset: number;
}

export interface TranscriptSegment {
  id: string;
  sequence: number;
  speaker_label: string;
  start_time: number;
  end_time: number;
  text: string;
  language: string | null;
  confidence: number | null;
  has_overlap: boolean;
}

export interface TranscriptResponse {
  recording_id: string;
  status: RecordingStatus;
  revision: number;
  text: string;
  segments: TranscriptSegment[];
  furigana: FuriganaMap;
}

export type AnalysisStatus = "completed" | "skipped" | "failed" | "stale";

export interface BilingualText {
  ja: string;
  zh_hk: string;
}

export interface BilingualTag extends BilingualText {}

export interface NaturalExpression {
  segment_sequence: number;
  start_time: number;
  end_time: number | null;
  speaker_label: string;
  original_ja: string;
  translation_zh_hk: string;
  usage_ja: string;
  usage_zh_hk: string;
}

export interface AnalysisHighlight {
  segment_sequence: number;
  start_time: number;
  end_time: number | null;
  speaker_label: string;
  original_ja: string;
  translation_zh_hk: string;
  reason_ja: string;
  reason_zh_hk: string;
}

export interface FuriganaToken {
  text: string;
  /** hiragana over this run; null when the run needs no reading */
  reading: string | null;
}

/** Reading runs keyed by the exact Japanese string they annotate. */
export type FuriganaMap = Record<string, FuriganaToken[]>;

export type BookmarkKind = "expression" | "highlight";

export interface Bookmark {
  id: string;
  kind: BookmarkKind;
  source_digest: string;
  original_ja: string;
  translation_zh_hk: string;
  /** usage for an expression, reason for a highlight */
  note_ja: string;
  note_zh_hk: string;
  speaker_label: string;
  start_time: number;
  end_time: number | null;
  /** null once the source recording has been deleted */
  recording_id: string | null;
  source_label: string;
  source_deleted_at: string | null;
  created_at: string;
}

export interface BookmarkListResponse {
  items: Bookmark[];
  furigana: FuriganaMap;
}

export interface BookmarkCreateRequest {
  kind: BookmarkKind;
  recording_id: string | null;
  original_ja: string;
  translation_zh_hk: string;
  note_ja: string;
  note_zh_hk: string;
  speaker_label: string;
  start_time: number;
  end_time: number | null;
}

export interface AnalysisResultV2 {
  description: BilingualText;
  summary: BilingualText | null;
  tags: BilingualTag[];
  natural_expressions: NaturalExpression[];
  highlights: AnalysisHighlight[];
}

export interface AnalysisResponse {
  recording_id: string;
  status: AnalysisStatus;
  provider: string;
  model: string | null;
  schema_version: string;
  revision: number;
  result: AnalysisResultV2 | null;
  job: JobStatusDetails | null;
  error: { code: string; message: string } | null;
  furigana: FuriganaMap;
}

export interface UploadRecordingResponse {
  recording_id: string;
  status: RecordingStatus;
  duplicate: boolean;
}
