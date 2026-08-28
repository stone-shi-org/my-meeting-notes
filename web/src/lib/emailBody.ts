/**
 * Split an email body into what the sender actually typed, the quoted history
 * below it, and a trailing signature.
 *
 * A plain-text body, already converted from HTML server-side — so there is no
 * markup to parse and no sanitizer on this path.
 *
 * The implementation scans **line by line for the earliest fold index, then cuts
 * once**. Deliberately not a global multiline regex: `^`/`$` semantics under
 * `/m` is where these implementations go wrong, and a single cut index is far
 * easier to reason about and to test.
 *
 * Nothing is ever discarded. `reply + quoted + signature` reconstitutes the
 * input, and there is a property test asserting exactly that — which is what
 * makes this file safe to keep tuning.
 */

export type FoldReason =
  | 'attribution'
  | 'quote-prefix'
  | 'original-message'
  | 'outlook-header';

export interface EmailBodyParts {
  /** What the sender wrote this time. */
  reply: string;
  /** Everything folded away, in its original order. Empty when nothing folded. */
  quoted: string;
  /** A trailing signature block, split off but never dropped. */
  signature: string;
  /** Which detector fired, or null when the whole body is the reply. */
  foldedBy: FoldReason | null;
}

/**
 * Below this, folding costs more than it saves: hiding three lines behind a
 * click is worse than just showing them.
 */
const MIN_FOLDABLE_CHARS = 400;
const MIN_FOLDABLE_LINES = 8;

/**
 * A `-- ` further than this from the end is a horizontal rule someone typed, not
 * a signature delimiter.
 */
const MAX_SIGNATURE_LINES = 8;

/**
 * Localised tails of the "On <date>, <person> wrote:" attribution line. A table
 * so adding a locale is one line rather than a rewrite; an unmatched locale
 * simply does not fold, which is the safe direction — nothing is lost but
 * length.
 */
const ATTRIBUTION_TAILS: RegExp[] = [
  /\bwrote:\s*$/i,
  /\bsent:\s*$/i,
  /a écrit\s*:\s*$/i,
  /schrieb:\s*$/i,
  /escribió:\s*$/i,
  /ha scritto:\s*$/i,
  /schreef:\s*$/i,
  /skrev:\s*$/i,
  /napisał[ao]?:\s*$/i,
  /写道:\s*$/,
];

/** Openers that can begin an attribution line. */
const ATTRIBUTION_OPENERS = /^\s*(?:on\b|am\b|le\b|el\b|il\b|op\b|den\b|>?\s*on\b)/i;

const ORIGINAL_MESSAGE =
  /^\s*-{2,}\s*(?:original message|forwarded message|weitergeleitete nachricht|mensaje original)\s*-{2,}\s*$/i;

/** Gmail's forward banner, whose dash count varies. */
const FORWARD_BANNER = /^\s*-{2,}\s*forwarded message\s*-{2,}\s*$/i;

const QUOTE_PREFIX = /^\s{0,3}>/;

const OUTLOOK_FROM = /^\s*(?:from|von|de|van|från)\s*:\s*\S/i;
const OUTLOOK_COMPANIONS = [
  /^\s*(?:sent|date|datum|enviado|verzonden|skickat)\s*:/i,
  /^\s*(?:to|an|para|aan|till)\s*:/i,
  /^\s*(?:cc|kopie)\s*:/i,
  /^\s*(?:subject|betreff|asunto|onderwerp|ämne)\s*:/i,
];

const SIGNATURE_DELIMITER = /^--\s?$/;

/** Fenced code blocks are skipped entirely — see `isFenceToggle`. */
const FENCE = /^\s*(?:```|~~~)/;

/** A 4-space or tab indent: a code block in a GitHub/CI notification. */
const INDENTED_CODE = /^(?: {4}|\t)/;

function isBlank(line: string): boolean {
  return line.trim() === '';
}

/**
 * Is line `i` an attribution line?
 *
 * Gmail wraps long ones, so this joins up to three lines before testing:
 *
 *     On Tue, 25 Aug 2026 at 09:14, Priya Raman <priya@x.com>
 *     wrote:
 */
function attributionLength(lines: string[], i: number): number {
  if (!ATTRIBUTION_OPENERS.test(lines[i])) return 0;
  let joined = lines[i];
  for (let span = 1; span <= 3; span += 1) {
    if (joined.length > 400) break;
    if (ATTRIBUTION_TAILS.some((tail) => tail.test(joined))) return span;
    if (i + span >= lines.length) break;
    joined = `${joined} ${lines[i + span].trim()}`;
  }
  return ATTRIBUTION_TAILS.some((tail) => tail.test(joined)) ? 4 : 0;
}

/**
 * A run of at least two `>`-prefixed lines starting at `i`.
 *
 * Two, not one: a single rhetorical `> like this` in the middle of a reply must
 * not fold everything after it. Blank lines inside the run are allowed, since
 * quoted blocks routinely contain them.
 */
function isQuoteRun(lines: string[], i: number): boolean {
  if (!QUOTE_PREFIX.test(lines[i])) return false;
  for (let j = i + 1; j < lines.length && j <= i + 4; j += 1) {
    if (QUOTE_PREFIX.test(lines[j])) return true;
    if (!isBlank(lines[j])) return false;
  }
  return false;
}

/**
 * An Outlook-style header block: `From:` plus at least two companion headers
 * within the next six lines.
 *
 * The companion requirement is what keeps "From: the design review, here's what
 * changed" from folding an entire message.
 */
function isOutlookHeader(lines: string[], i: number): boolean {
  if (!OUTLOOK_FROM.test(lines[i])) return false;
  let hits = 0;
  for (let j = i + 1; j < lines.length && j <= i + 6; j += 1) {
    if (OUTLOOK_COMPANIONS.some((re) => re.test(lines[j]))) hits += 1;
  }
  return hits >= 2;
}

function isFenceToggle(line: string): boolean {
  return FENCE.test(line);
}

function splitSignature(reply: string): { body: string; signature: string } {
  const lines = reply.split('\n');
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (!SIGNATURE_DELIMITER.test(lines[i])) continue;
    // Too much after it: a rule someone typed, not a sig delimiter.
    if (lines.length - i - 1 > MAX_SIGNATURE_LINES) return { body: reply, signature: '' };
    return {
      body: lines.slice(0, i).join('\n'),
      signature: lines.slice(i).join('\n'),
    };
  }
  return { body: reply, signature: '' };
}

export function splitQuoted(body: string | null | undefined): EmailBodyParts {
  const text = (body ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const none: EmailBodyParts = { reply: text, quoted: '', signature: '', foldedBy: null };
  if (!text.trim()) return none;

  const lines = text.split('\n');

  // Short bodies are never folded, whatever they contain.
  if (text.length < MIN_FOLDABLE_CHARS && lines.length < MIN_FOLDABLE_LINES) {
    const { body: head, signature } = splitSignature(text);
    return { reply: head, quoted: '', signature, foldedBy: null };
  }

  let cut = -1;
  let reason: FoldReason | null = null;
  let inFence = false;
  let attributionAt = -1;

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    if (isFenceToggle(line)) {
      inFence = !inFence;
      continue;
    }
    // Code is the case that will actually bite: GitHub and CI notification mail
    // is full of `>` inside fenced and indented blocks.
    if (inFence || INDENTED_CODE.test(line)) continue;

    if (ORIGINAL_MESSAGE.test(line) || FORWARD_BANNER.test(line)) {
      cut = i;
      reason = 'original-message';
      break;
    }

    const span = attributionLength(lines, i);
    if (span > 0) {
      cut = i;
      reason = 'attribution';
      break;
    }

    if (isQuoteRun(lines, i)) {
      // If an attribution sits just above, fold from there instead, so
      // "On ... wrote:" goes with its quote rather than dangling above it.
      cut = attributionAt >= 0 && i - attributionAt <= 2 ? attributionAt : i;
      reason = 'quote-prefix';
      break;
    }

    if (isOutlookHeader(lines, i)) {
      cut = i;
      reason = 'outlook-header';
      break;
    }

    if (ATTRIBUTION_OPENERS.test(line)) attributionAt = i;
  }

  if (cut < 0) {
    const { body: head, signature } = splitSignature(text);
    return { reply: head, quoted: '', signature, foldedBy: null };
  }

  const replyRaw = lines.slice(0, cut).join('\n');
  const quoted = lines.slice(cut).join('\n');

  // An all-quote message: there is nothing left to show, so show all of it.
  if (!replyRaw.trim()) return none;

  const { body: reply, signature } = splitSignature(replyRaw);
  return { reply, quoted, signature, foldedBy: reason };
}

/** How many lines were folded, for the disclosure label. */
export function quotedLineCount(parts: EmailBodyParts): number {
  if (!parts.quoted) return 0;
  return parts.quoted.split('\n').filter((l) => !isBlank(l)).length;
}
