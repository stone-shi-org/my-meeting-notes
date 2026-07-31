import { notifyUnauthorized } from '@/lib/api';
import { ApiError, type ApiErrorBody, type ChatMessage } from '@/types/api';

/**
 * Split a growing text buffer into complete SSE frames (`event:`/`data:`
 * lines terminated by a blank line), returning whatever's left as `rest` for
 * the next chunk -- a `fetch` read can split a frame across two chunks, so
 * this has to be resumable rather than parsing the whole buffer at once.
 * Comment lines (keepalives) are skipped, not returned as frames.
 */
export function parseSseFrames(buffer: string): {
  frames: { event: string; data: string }[];
  rest: string;
} {
  const frames: { event: string; data: string }[] = [];
  let rest = buffer;

  let boundary: number;
  while ((boundary = rest.indexOf('\n\n')) !== -1) {
    const block = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    if (!block || block.startsWith(':')) continue;

    let event = 'message';
    let data: string | null = null;
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice('event:'.length).trim();
      else if (line.startsWith('data:')) data = line.slice('data:'.length).trim();
    }
    if (data !== null) frames.push({ event, data });
  }

  return { frames, rest };
}

export interface ChatStreamHandlers<T = ChatMessage> {
  onToken: (text: string) => void;
  onDone: (message: T) => void;
  onError: (error: { code: string; message: string }) => void;
}

/**
 * POST a chat message and read the SSE reply. Deliberately separate from
 * lib/api.ts's request() -- that helper always does one JSON in, one JSON
 * out, and every other endpoint in the app still wants exactly that.
 * EventSource can't be used here since it can't send a POST body.
 *
 * `path` is the endpoint below `/api` (e.g. `/threads/1/chat` or
 * `/meetings/1/chat`) -- both thread chat and meeting chat share this same
 * SSE contract (`token`/`done`/`error` events), only the digest behind it
 * differs.
 */
export async function streamChat<T = ChatMessage>(
  path: string,
  message: string,
  handlers: ChatStreamHandlers<T>,
  signal?: AbortSignal,
  model?: string | null,
): Promise<void> {
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(model ? { message, model } : { message }),
    signal,
  });

  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    let errMessage = response.statusText;
    let code = 'http_error';
    try {
      body = (await response.json()) as ApiErrorBody;
      errMessage = body?.error?.message ?? errMessage;
      code = body?.error?.code ?? code;
    } catch {
      /* non-JSON error body */
    }
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, code, errMessage, body);
  }

  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) return;

    buffer += decoder.decode(value, { stream: true });
    const { frames, rest } = parseSseFrames(buffer);
    buffer = rest;

    for (const frame of frames) {
      const data = JSON.parse(frame.data);
      if (frame.event === 'token') handlers.onToken(data.text);
      else if (frame.event === 'done') handlers.onDone(data as T);
      else if (frame.event === 'error') handlers.onError(data);
    }
  }
}
