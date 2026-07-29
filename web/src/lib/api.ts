import { ApiError, type ApiErrorBody } from '@/types/api';

type Query = Record<string, string | number | boolean | null | undefined>;

/** Fires on any 401 so the shell can bounce to /login from anywhere. */
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

/** Fires on 409 password_change_required, the forced-change sentinel. */
let onPasswordChangeRequired: (() => void) | null = null;
export function setPasswordChangeHandler(fn: () => void) {
  onPasswordChangeRequired = fn;
}

function qs(query?: Query): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value));
    }
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

async function request<T>(
  method: string,
  path: string,
  opts: { body?: unknown; query?: Query; raw?: boolean } = {},
): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'include',
    headers: {},
  };

  if (opts.body !== undefined) {
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }

  const response = await fetch(`/api${path}${qs(opts.query)}`, init);

  if (response.status === 204) return undefined as T;

  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    let message = response.statusText;
    let code = 'http_error';
    try {
      body = (await response.json()) as ApiErrorBody;
      message = body?.error?.message ?? message;
      code = body?.error?.code ?? code;
    } catch {
      /* non-JSON error body */
    }

    if (response.status === 401) onUnauthorized?.();
    if (response.status === 409 && code === 'password_change_required') {
      onPasswordChangeRequired?.();
    }

    throw new ApiError(response.status, code, message, body);
  }

  if (opts.raw) return (await response.text()) as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>('GET', path, { query }),
  getText: (path: string, query?: Query) =>
    request<string>('GET', path, { query, raw: true }),
  post: <T>(path: string, body?: unknown, query?: Query) =>
    request<T>('POST', path, { body, query }),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, { body }),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, { body }),
  del: <T>(path: string, query?: Query) => request<T>('DELETE', path, { query }),
};

export interface UploadResult {
  meeting_id: number;
  thread_id: number;
  job_id: string;
  bytes: number;
}

/**
 * Multipart upload with progress.
 *
 * XHR rather than fetch: fetch still cannot report upload progress, and these
 * files run to 100 MB -- a silent bar for two minutes is not acceptable.
 *
 * The path is a parameter because audio arrives two ways: creating a meeting
 * around it, and adding it to a meeting that already exists.
 */
export function uploadAudio(
  path: string,
  form: FormData,
  onProgress?: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api${path}`);
    xhr.withCredentials = true;

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded, e.total);
    };

    xhr.onload = () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(xhr.responseText);
      } catch {
        parsed = undefined;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(parsed as never);
      } else {
        const body = parsed as ApiErrorBody | undefined;
        if (xhr.status === 401) onUnauthorized?.();
        reject(
          new ApiError(
            xhr.status,
            body?.error?.code ?? 'upload_failed',
            body?.error?.message ?? `Upload failed (${xhr.status})`,
            body,
          ),
        );
      }
    };

    xhr.onerror = () => reject(new ApiError(0, 'network_error', 'Network error during upload'));
    xhr.onabort = () => reject(new ApiError(0, 'aborted', 'Upload cancelled'));

    signal?.addEventListener('abort', () => xhr.abort());
    xhr.send(form);
  });
}

/** Create a meeting around a new recording. */
export function uploadMeeting(
  form: FormData,
  onProgress?: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadResult> {
  return uploadAudio('/meetings/upload', form, onProgress, signal);
}

/** Give an existing meeting the recording it was created without. */
export function uploadMeetingAudio(
  meetingId: number,
  form: FormData,
  onProgress?: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadResult> {
  return uploadAudio(`/meetings/${meetingId}/audio`, form, onProgress, signal);
}
