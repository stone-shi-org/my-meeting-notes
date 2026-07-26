/**
 * Speaker identity colours.
 *
 * Assignment is derived from the diarization id, never from the speaker's
 * position in a list -- otherwise filtering the transcript to two speakers
 * repaints them, which reads as a bug.
 *
 * Colour is a redundant accelerator only: SpeakerChip never renders a mark
 * without the speaker's name or initials beside it.
 */

export const SPEAKER_SLOTS = 8;

function hash(value: string): number {
  let h = 0;
  for (let i = 0; i < value.length; i++) {
    h = (h * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

/** Stable slot 0..7 for a diarization speaker id such as `SPEAKER_03`. */
export function speakerSlot(speakerId: string): number {
  const m = /(\d+)$/.exec(speakerId);
  if (m) return Number(m[1]) % SPEAKER_SLOTS;
  return hash(speakerId) % SPEAKER_SLOTS;
}

/** CSS custom properties to spread onto a speaker-coloured element. */
export function speakerVars(speakerId: string): Record<string, string> {
  const slot = speakerSlot(speakerId);
  return {
    '--sp': `var(--speaker-${slot})`,
    '--sp-ink': `var(--speaker-${slot}-ink)`,
  };
}

/** `Alice Chen` -> `AC`; `SPEAKER_00` -> `S0`. Always two characters or fewer. */
export function initials(name: string): string {
  const cleaned = name.replace(/^SPEAKER[_-]?/i, 'S').trim();
  const parts = cleaned.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}
