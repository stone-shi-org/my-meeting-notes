import { describe, expect, it } from 'vitest';
import { buildIndex, findIndex } from '../transcriptIndex';

// Deliberately gapped, mirroring real diarizer output: 1.66 -> 1.84 is a gap.
const SEGMENTS = [
  { start: 0, end: 1.66 },
  { start: 1.84, end: 4.75 },
  { start: 5.0, end: 20.0 },
  { start: 20.68, end: 54.85 },
];

const idx = buildIndex(SEGMENTS);

describe('findIndex', () => {
  it('finds the covering segment without a hint', () => {
    expect(findIndex(idx, 0)).toBe(0);
    expect(findIndex(idx, 3)).toBe(1);
    expect(findIndex(idx, 10)).toBe(2);
    expect(findIndex(idx, 30)).toBe(3);
  });

  it('returns -1 before the first segment starts', () => {
    const late = buildIndex([{ start: 5, end: 10 }]);
    expect(findIndex(late, 1)).toBe(-1);
  });

  it('takes the fast path when the playhead stays inside the hinted segment', () => {
    expect(findIndex(idx, 3.0, 1)).toBe(1);
  });

  it('takes the fast path when the playhead steps into the next segment', () => {
    expect(findIndex(idx, 5.5, 1)).toBe(2);
  });

  it('holds the previous segment across a gap rather than flickering to -1', () => {
    // 1.70 falls between segment 0 (ends 1.66) and segment 1 (starts 1.84).
    expect(findIndex(idx, 1.7, 0)).toBe(0);
    expect(findIndex(idx, 1.7)).toBe(0);
  });

  it('treats start as inclusive and end as exclusive', () => {
    expect(findIndex(idx, 1.84)).toBe(1);
    expect(findIndex(idx, 4.75)).toBe(1); // in the gap, holds segment 1
    expect(findIndex(idx, 5.0)).toBe(2);
  });

  it('holds the last segment past the end of the audio', () => {
    expect(findIndex(idx, 9999)).toBe(3);
    expect(findIndex(idx, 9999, 3)).toBe(3);
  });

  it('handles an empty transcript', () => {
    expect(findIndex(buildIndex([]), 5)).toBe(-1);
  });

  it('handles a single segment', () => {
    const one = buildIndex([{ start: 0, end: 10 }]);
    expect(findIndex(one, 5)).toBe(0);
    expect(findIndex(one, 5, 0)).toBe(0);
  });

  it('recovers from a stale hint after a backwards seek', () => {
    expect(findIndex(idx, 0.5, 3)).toBe(0);
  });

  it('agrees with the hinted path for every position', () => {
    let hint = -1;
    for (let t = 0; t < 60; t += 0.1) {
      const hinted = findIndex(idx, t, hint);
      const cold = findIndex(idx, t);
      expect(hinted).toBe(cold);
      hint = hinted;
    }
  });
});
