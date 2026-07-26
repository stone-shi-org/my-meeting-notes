/**
 * Non-speech markers emitted by the diarizer, e.g. `[Environmental Sounds]`.
 *
 * These are whole-segment markers only. A segment that merely *contains* a
 * bracketed aside is still speech and must not be filtered out.
 */

const NON_SPEECH = new Set([
  'environmental sounds',
  'silence',
  'music',
  'noise',
  'background noise',
  'inaudible',
  'applause',
  'laughter',
]);

export function isNonSpeech(text: string): boolean {
  const trimmed = text.trim();
  const m = /^\[([^\]]+)\]$/.exec(trimmed);
  if (!m) return false;
  return NON_SPEECH.has(m[1].trim().toLowerCase());
}
