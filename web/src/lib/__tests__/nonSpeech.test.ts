import { describe, expect, it } from 'vitest';
import { isNonSpeech } from '../nonSpeech';

describe('isNonSpeech', () => {
  it('matches the marker the diarizer actually emits', () => {
    expect(isNonSpeech('[Environmental Sounds]')).toBe(true);
  });

  it('is case and whitespace tolerant', () => {
    expect(isNonSpeech('  [environmental sounds]  ')).toBe(true);
    expect(isNonSpeech('[SILENCE]')).toBe(true);
  });

  it('matches the other known markers', () => {
    for (const m of ['[Music]', '[Noise]', '[Inaudible]', '[Applause]', '[Laughter]']) {
      expect(isNonSpeech(m), m).toBe(true);
    }
  });

  it('leaves ordinary speech alone', () => {
    expect(isNonSpeech('Yes, this is Stan speaking.')).toBe(false);
    expect(isNonSpeech('')).toBe(false);
  });

  it('does not filter speech that merely contains a bracketed aside', () => {
    expect(isNonSpeech('So then [laughter] we shipped it')).toBe(false);
  });

  it('does not match an unrecognised bracketed token', () => {
    expect(isNonSpeech('[Q3 Planning]')).toBe(false);
  });
});
