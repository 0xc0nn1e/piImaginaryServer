export type RecordingStatus = "uploaded" | "queued" | "processing" | "completed" | "failed";

export type JobStatus = "queued" | "processing" | "completed" | "failed";

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
  status: JobStatus;
  stage: JobStage;
  attempt_count: number;
  max_attempts: number;
  available_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: JobError | null;
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
  text: string;
  segments: TranscriptSegment[];
}
