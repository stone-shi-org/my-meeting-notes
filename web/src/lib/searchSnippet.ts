/**
 * Search snippets: `\u0002…\u0003` sentinels to `<mark>`, without ever
 * touching HTML.
 *
 * The server marks highlighted terms with two control characters rather than
 * with `<mark>` tags precisely so the client never has to parse markup out of
 * user content. A snippet is an email body, a note, a transcript line -- any
 * of which can contain `<b>` or `<script>` as *text*. Splitting on the
 * sentinels and handing React plain strings keeps all of it text: React
 * escapes children, and nothing here goes near `dangerouslySetInnerHTML`.
 */
import { createElement, type ReactNode } from 'react';

export const MARK_OPEN = '\u0002';
export const MARK_CLOSE = '\u0003';

export interface SnippetPart {
  text: string;
  mark: boolean;
}

/**
 * Split a snippet into plain and highlighted runs.
 *
 * Unbalanced input is expected rather than exotic: FTS5's `snippet()` trims
 * to a token window, and the cut can fall between an open and its close. So:
 * a stray close is dropped, a second open inside a mark is ignored, and an
 * open never closed highlights to the end -- it marked a real term that the
 * window cut short. Empty runs are dropped so `<mark></mark>` never renders.
 */
export function parseSnippet(snippet: string | null | undefined): SnippetPart[] {
  if (!snippet) return [];
  const parts: SnippetPart[] = [];
  let buffer = '';
  let open = false;

  const flush = (mark: boolean) => {
    if (!buffer) return;
    const last = parts[parts.length - 1];
    // Adjacent runs of the same kind (e.g. either side of a dropped stray
    // close) read as one; keeping them separate would only add elements.
    if (last && last.mark === mark) last.text += buffer;
    else parts.push({ text: buffer, mark });
    buffer = '';
  };

  for (const ch of snippet) {
    if (ch === MARK_OPEN) {
      if (!open) {
        flush(false);
        open = true;
      }
    } else if (ch === MARK_CLOSE) {
      if (open) {
        flush(true);
        open = false;
      }
    } else {
      buffer += ch;
    }
  }
  flush(open);
  return parts;
}

/** The snippet with every sentinel removed, for places that cannot highlight. */
export function stripSentinels(text: string | null | undefined): string {
  return (text ?? '').split(MARK_OPEN).join('').split(MARK_CLOSE).join('');
}

/**
 * The highlight. Text colour is an `*-ink` token (the only kind allowed to
 * carry text besides the `--fg` family) on its own soft plane, not the
 * browser's default yellow, which ignores the dark theme entirely.
 */
export const MARK_CLASS = 'rounded-sm bg-warning-soft px-0.5 font-medium text-warning-ink';

/**
 * React nodes for a snippet. Highlighted runs are `<mark>`s carrying
 * `markClassName`; everything else is a bare string, which React escapes.
 */
export function renderSnippet(
  snippet: string | null | undefined,
  markClassName = MARK_CLASS,
): ReactNode[] {
  return parseSnippet(snippet).map((part, i) =>
    part.mark ? createElement('mark', { key: i, className: markClassName }, part.text) : part.text,
  );
}
