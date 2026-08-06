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
      case 'NO_INTEGRATIONS':
      case 'NEEDS_REAUTH':
      case 'provider_error':
      // MCP codes stay mapped: they are still raised by the MCP provider, and a
      // cached bundle can surface them too.
      case 'MCP_TIMEOUT':
      case 'mcp_error':
        return '/settings/integrations';
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
  note_count: number;
  /** Auto-attached items nobody has opened. Non-zero lights the dot. */
  unread_count: number;
  /** When the periodic sweep last looked at this thread. */
  auto_match_at: string | null;
  auto_match_error: string | null;
  /** Cached LLM suggestion for what to do next on this thread. */
  next_step: string | null;
  next_step_generated_at: string | null;
  /** True when a meeting/email/event has been added since next_step was
   * generated, or nothing has been generated yet. */
  next_step_stale: boolean;
  /** Which home-screen group the thread sits in. null is "Ungrouped". */
  group_id: number | null;
}

/** A folder over threads on the home screen. "Ungrouped" is not one of these —
 * it is the threads whose `group_id` is null, which is where they all start. */
export interface ThreadGroup {
  id: number;
  owner_id: number;
  name: string;
  /** Every thread in the group, unfiltered. Shown while the section's own
   * query is still in flight, and counted in the delete confirmation. */
  thread_count: number;
  created_at: string;
  updated_at: string;
}

/* -------------------------------------------------------------------------- */
/* Development data — only present when MMN_DEV_PROVIDER_ENABLED is set        */
/* -------------------------------------------------------------------------- */

/** When an authored item happens. Only the last two survive contact with time:
 * an absolute date falls out of the match window within a couple of months. */
export type DevDateMode = 'absolute' | 'relative' | 'anchored';

interface DevItemBase {
  id: number;
  integration_id: number;
  date_mode: DevDateMode;
  /** Set only when date_mode is 'absolute'. */
  at: string | null;
  /** Signed. From now, or from the anchor meeting's start. */
  offset_minutes: number | null;
  anchor_meeting_id: number | null;
  /** Ground truth: should a correct matcher pick this up? Nothing branches on
   * it — it is the answer key for judging a run. */
  expected_relevant: boolean;
  created_at: string;
  updated_at: string;
}

export interface DevEmail extends DevItemBase {
  subject: string;
  sender: string | null;
  snippet: string | null;
  account: string | null;
  /** Emit the date the way Gmail does, to exercise timestamp normalisation. */
  rfc2822_date: boolean;
}

export interface DevEvent extends DevItemBase {
  summary: string;
  description: string | null;
  location: string | null;
  attendees: string[];
  calendar_name: string | null;
  event_type: string | null;
  duration_minutes: number;
  /** A bare date with no time, like a real all-day event. */
  all_day: boolean;
  /** Expands into N weekly instances sharing one source_uid. */
  repeat_weekly: number;
}

/** An LLM-drafted item, not yet written. Accepting one POSTs it through the
 * same create route the manual form uses. */
export interface DevDraft {
  kind: 'emails' | 'events';
  date_mode: DevDateMode;
  anchor_meeting_id: number | null;
  offset_minutes: number;
  expected_relevant: boolean;
  /** The model's one-line reason for inventing it. */
  note: string;
  subject?: string;
  sender?: string | null;
  snippet?: string | null;
  summary?: string;
  description?: string | null;
  location?: string | null;
  attendees?: string[];
  duration_minutes?: number;
}

/** Returned by POST /threads/{id}/next-step. */
export interface NextStepResult {
  next_step: string | null;
  next_step_generated_at: string | null;
  next_step_stale: boolean;
  error: string | null;
}

/** Attached by the sweep rather than by a person, and whether it is still new. */
export interface Attached {
  auto_attached?: boolean;
  seen_at?: string | null;
  unread?: boolean;
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
  /** Derived server-side: fraction of total talk time, and that time as a clock. */
  share: number;
  duration_human: string;
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

export interface CalendarEvent extends Attached {
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
  /** Display names, organizer first. Prefilled as speaker names. */
  attendees?: string[];
  relevance_score: number | null;
  relevance_reason: string | null;
  suggested?: boolean;
  attached_at?: string;
}

/** One event on the home screen's upcoming list. */
export interface UpcomingEvent {
  uid: string;
  summary: string | null;
  description: string | null;
  location: string | null;
  start: string | null;
  end: string | null;
  attendees: string[];
  calendar_name: string | null;
  account: string | null;
  type: string | null;
  url: string | null;
  source_uid: string | null;
  provider: string | null;
  integration_id: number | null;
  /** Set once a meeting has been created from it; null while it is still open. */
  attached: { thread_id: number; meeting_id: number | null; meeting_title: string | null } | null;
}

export interface UpcomingList {
  /** Calendar accounts searched. Zero means "connect one" rather than "none due". */
  connected: number;
  start: string;
  end: string;
  events: UpcomingEvent[];
  /** Set only when every calendar failed; per-account detail is below. */
  error: string | null;
  source_errors: {
    kind: string;
    provider: string;
    integration_id: number;
    account: string;
    error: string;
  }[];
}

export interface Email extends Attached {
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
  /** Provider-owned deep link. Null for IMAP, which has no web UI to link to. */
  url?: string | null;
  /** The real RFC 2822 Message-ID, for the Gmail fallback link. */
  rfc_message_id?: string | null;
  provider?: string | null;
}

export interface MatchRun {
  id: number;
  status: 'ok' | 'partial' | 'failed';
  query: {
    keywords: string[];
    calendar: { query: string; start_date: string; end_date: string };
    email: { query: string };
    sources?: { kind: string; provider: string; integration_id: number; account: string }[];
  };
  events: CalendarEvent[];
  emails: Email[];
  notes: string;
  model: string | null;
  /** Aggregates, set only when every account of that kind failed. */
  calendar_error: string | null;
  email_error: string | null;
  /** Per-account detail behind those aggregates. */
  source_errors?: {
    kind: string;
    provider: string;
    integration_id: number;
    account: string;
    error: string;
  }[];
  error: string | null;
  created_at: string;
}

/** One run of the periodic sweep, returned by "Check now". */
export interface FollowUpResult {
  thread_id: number;
  skipped: 'no_integrations' | 'nothing_new' | null;
  candidates: number;
  attached_events: number;
  attached_emails: number;
  error: string | null;
}

/**
 * The one kind of attached document this app writes rather than fetches.
 *
 * Unlike an email or a calendar event there is nothing external behind it, so
 * it has no uid, no provider and no relevance score -- and it is never unread,
 * because only the sweep creates unread rows and nothing sweeps notes into
 * existence.
 */
export interface Note {
  id: number;
  thread_id: number;
  /** Null means the note is filed on the thread, not on one meeting. */
  meeting_id: number | null;
  title: string;
  /** Markdown. Rendered as such everywhere it is displayed. */
  body: string;
  source: 'ai_chat' | 'manual';
  /** The chat model whose reply this was, when it came from one. */
  model: string | null;
  /** What named it. Null means typed by hand, or the title call failed and the
   * first line of the body was used. */
  title_model: string | null;
  created_by: number | null;
  created_at: string;
  updated_at: string;
}

export interface TimelineItem {
  kind: 'meeting' | 'event' | 'email' | 'note';
  at: string | null;
  id: number;
  payload: Meeting | CalendarEvent | Email | Note;
}

export interface ChatMessage {
  id: number;
  thread_id: number;
  role: 'user' | 'assistant';
  content: string;
  model: string | null;
  created_at: string;
}

export interface MeetingChatMessage {
  id: number;
  meeting_id: number;
  role: 'user' | 'assistant';
  content: string;
  model: string | null;
  created_at: string;
}

/** One connected calendar/email account. Always the caller's own. */
export interface Integration {
  id: number;
  provider: string;
  provider_label: string;
  supported_kinds: string[];
  account_key: string;
  account_label: string | null;
  calendar_enabled: boolean;
  email_enabled: boolean;
  enabled: boolean;
  /** 'none' is the Development provider: there is no credential to hold. */
  auth_type: 'oauth2' | 'password' | 'token' | 'none';
  /** Non-secret settings only. */
  config: Record<string, unknown>;
  has_secret: boolean;
  /** Masked tail, e.g. ••••1234. Echo it back to leave the secret unchanged. */
  secret_preview: string | null;
  status: 'ok' | 'error' | 'unverified' | 'reauth_required';
  scopes: string | null;
  token_expires_at: string | null;
  last_test: { at: string | null; ok: boolean | null; error: string | null };
  created_at: string;
  updated_at: string;
}

export interface ProviderSpec {
  id: string;
  label: string;
  kinds: string[];
  /** 'none' is the Development provider: there is no credential to hold. */
  auth_type: 'oauth2' | 'password' | 'token' | 'none';
  docs_url: string;
}

/** Drives the enabled/disabled state of the match button. */
export interface IntegrationSummary {
  calendar: number;
  email: number;
  needs_reauth: { id: number; provider: string; account_label: string }[];
}

/** One leg of a connection test -- a provider can be half-working. */
export interface IntegrationCheck {
  name: string;
  ok: boolean;
  error: string | null;
}

export interface IntegrationTestResult {
  ok: boolean;
  latency_ms: number;
  checks: IntegrationCheck[];
  error: string | null;
}

export interface SettingEntry {
  value: string | number | boolean | string[] | null;
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

export interface VersionInfo {
  hash: string;
  timestamp: string | null;
}

export interface Health {
  status: string;
  db: { ok: boolean; error: string | null; path: string };
  ffmpeg: string | null;
  workers: number;
  version: VersionInfo;
}
