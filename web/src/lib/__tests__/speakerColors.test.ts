import { describe, expect, it } from 'vitest';
import { SPEAKER_SLOTS, initials, speakerSlot, speakerVars } from '../speakerColors';

describe('speakerSlot', () => {
  it('derives the slot from the numeric suffix', () => {
    expect(speakerSlot('SPEAKER_00')).toBe(0);
    expect(speakerSlot('SPEAKER_01')).toBe(1);
    expect(speakerSlot('SPEAKER_07')).toBe(7);
  });

  it('wraps past the number of slots', () => {
    expect(speakerSlot('SPEAKER_08')).toBe(0);
    expect(speakerSlot('SPEAKER_11')).toBe(3);
  });

  it('is stable by id, not by list position', () => {
    // The bug this guards: filtering a transcript down to a subset of speakers
    // must not repaint them.
    const all = ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03'];
    const filtered = ['SPEAKER_02', 'SPEAKER_03'];

    expect(filtered.map(speakerSlot)).toEqual(
      all.filter((s) => filtered.includes(s)).map(speakerSlot),
    );
    expect(speakerSlot('SPEAKER_02')).toBe(2);
  });

  it('is deterministic for ids with no numeric suffix', () => {
    const a = speakerSlot('narrator');
    const b = speakerSlot('narrator');
    expect(a).toBe(b);
    expect(a).toBeGreaterThanOrEqual(0);
    expect(a).toBeLessThan(SPEAKER_SLOTS);
  });
});

describe('speakerVars', () => {
  it('maps to the CSS variables for its slot', () => {
    expect(speakerVars('SPEAKER_01')).toEqual({
      '--sp': 'var(--speaker-1)',
      '--sp-ink': 'var(--speaker-1-ink)',
    });
  });
});

describe('initials', () => {
  it('takes first and last initials of a real name', () => {
    expect(initials('Alice Chen')).toBe('AC');
    expect(initials('Priya Raman')).toBe('PR');
    expect(initials('Ana Maria Vidal')).toBe('AV');
  });

  it('handles a single word', () => {
    expect(initials('Bob')).toBe('BO');
  });

  it('renders raw diarization ids compactly', () => {
    expect(initials('SPEAKER_00')).toBe('S0');
    expect(initials('SPEAKER_12')).toBe('S1');
  });

  it('never returns an empty string', () => {
    expect(initials('')).toBe('?');
    expect(initials('   ')).toBe('?');
  });
});
