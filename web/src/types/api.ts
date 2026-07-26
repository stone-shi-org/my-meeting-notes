export interface Paginated<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    server?: string;
    transport?: string;
    details?: unknown;
  };
}

export class ApiError extends Error {
  status: number;
  code: string;
  body?: ApiErrorBody;

  constructor(status: number, code: string, message: string, body?: ApiErrorBody) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }

  /** The backend flags these so the UI can deep-link to the right settings tab. */
  get settingsHint(): string | null {
    switch (this.code) {
      case 'DIARIZATION_UNREACHABLE':
        return '/settings/diarization';
      case 'LLM_AUTH_FAILED':
      case 'llm_error':
        return '/settings/llm';
      case 'MCP_TIMEOUT':
      case 'mcp_error':
        return '/settings/mcp';
      default:
        return null;
    }
  }
}

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  is_admin: boolean;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface Thread {
  id: number;
  owner_id: number;
  title: string;
  description: string | null;
  archived: boolean;
  created_at: string;
  updated_at: string;
  meeting_count: number;
  last_meeting_at: string | null;
  email_count: number;
  event_count: number;
}

export interface Meeting {
  id: number;
  thread_id: number;
  owner_id: number;
  title: string;
  meeting_at: string | null;
  status: 'new' | 'processing' | 'ready' | 'failed';
  original_filename: string | null;
  original_bytes: number | null;
  audio_duration_sec: number | null;
  audio_sample_rate: number | null;
  audio_channels: number | null;
  audio_converted: boolean;
  has_audio: boolean;
  has_transcript: boolean;
  has_summary: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
  summary_tldr: string | null;
  open_action_items: number;
  speaker_count: number;
}

export interface Segment {
  id: number;
  speaker: string;
  speaker_name: string;
  label: string | null;
  start: number;
  end: number;
  text: string;
  non_speech: boolean;
}

export interface Speaker {
  id: string;
  label: string | null;
  display_name: string | null;
  color: string | null;
  total_speech_duration: number | null;
  segment_count: number | null;
  share?: number;
  duration_human?: string;
}

export interface Transcript {
  task: string | null;
  duration: number | null;
  num_speakers: number | null;
  speakers: Speaker[];
  segments: Segment[];
}

export interface ActionItem {
  id: number;
  summary_id: number;
  meeting_id: number;
  idx: number;
  text: string;
  owner_label: string | null;
  owner_speaker_id: string | null;
  due_text: string | null;
  due_date: string | null;
  priority: 'high' | 'medium' | 'low' | null;
  confidence: number | null;
  status: 'open' | 'done' | 'dropped';
  done_at: string | null;
}

export interface Summary {
  id: number;
  meeting_id: number;
  version: number;
  is_current: boolean;
  status: string;
  model: string;
  llm_base_url: string | null;
  temperature: number | null;
  prompt_name: string;
  prompt_version: string | null;
  prompt_sha256: string;
  prompt_text?: string;
  tldr: string | null;
  summary_md: string | null;
  title_suggestion: string | null;
  topics: string[];
  key_decisions: { decision: string; context: string; made_by: string }[];
  open_questions: string[];
  participants: { speaker: string; inferred_name: string; evidence: string }[];
  prompt_tokens: number | null;
  completion_tokens: number | null;
  duration_sec: number | null;
  transcript_sha256: string | null;
  error: string | null;
  created_at: string;
  action_items: ActionItem[];
  stale?: boolean;
}

export interface SummaryVersion {
  id: number;
  version: number;
  is_current: boolean;
  status: string;
  model: string;
  prompt_name: string;
  prompt_version: string | null;
  prompt_sha256: string;
  tldr: string | null;
  created_at: string;
  duration_sec: number | null;
}

export type JobStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export interface JobStage {
  key: string;
  label: string;
  weight: number;
}

export interface Job {
  id: string;
  type: 'ingest' | 'diarize' | 'summarize' | 'match';
  status: JobStatus;
  stage: string | null;
  progress: number;
  meeting_id: number | null;
  thread_id: number | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  error_stage: string | null;
  attempts: number;
  max_attempts: number;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  stages: JobStage[];
}

export interface JobEvent {
  id: number;
  ts: string;
  stage: string | null;
  level: 'info' | 'warn' | 'error';
  message: string;
  progress: number | null;
}

export interface CalendarEvent {
  id?: number;
  uid: string;
  url: string | null;
  summary: string | null;
  description: string | null;
  location: string | null;
  start?: string | null;
  end?: string | null;
  start_at?: string | null;
  end_at?: string | null;
  calendar_name: string | null;
  account: string | null;
  type?: string | null;
  relevance_score: number | null;
  relevance_reason: string | null;
  suggested?: boolean;
  attached_at?: string;
}

export interface Email {
  id?: number | string;
  message_id: string;
  sender: string | null;
  subject: string | null;
  date: string | null;
  snippet: string | null;
  account: string | null;
  triage_level: number | null;
  tag: string | null;
  summary: string | null;
  score: number | null;
  relevance_score: number | null;
  relevance_reason: string | null;
  suggested?: boolean;
  attached_at?: string;
}

export interface MatchRun {
  id: number;
  status: 'ok' | 'partial' | 'failed';
  query: {
    keywords: string[];
    calendar: { query: string; start_date: string; end_date: string };
    email: { query: string };
  };
  events: CalendarEvent[];
  emails: Email[];
  notes: string;
  model: string | null;
  calendar_error: string | null;
  email_error: string | null;
  error: string | null;
  created_at: string;
}

export interface TimelineItem {
  kind: 'meeting' | 'event' | 'email';
  at: string | null;
  id: number;
  payload: Meeting | CalendarEvent | Email;
}

export interface McpServer {
  name: string;
  kind: string;
  transport: 'sse' | 'stdio';
  enabled: boolean;
  base_url: string | null;
  auth_token: string | null;
  has_token: boolean;
  command: string | null;
  args: string[];
  cwd: string | null;
  env: Record<string, string>;
  tool_name: string;
  default_profile: string | null;
  timeout_sec: number;
  last_test: {
    at: string | null;
    ok: boolean | null;
    error: string | null;
    tools: string[];
  };
}

export interface SettingEntry {
  value: string | number | boolean | null;
  type: string;
  is_secret: boolean;
  overridden: boolean;
}

export interface PromptSummary {
  name: string;
  version: string;
  description: string | null;
  sha256: string;
  modified_at: number;
  required_placeholders: string[];
}

export interface PromptDetail extends PromptSummary {
  body: string;
  meta: Record<string, unknown>;
  system: string;
  user: string;
}

export interface Health {
  status: string;
  db: { ok: boolean; error: string | null; path: string };
  ffmpeg: string | null;
  workers: number;
  version: { hash: string; timestamp: string | null };
}
