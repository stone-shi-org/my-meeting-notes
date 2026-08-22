import { describe, expect, it } from 'vitest';
import { parseChunkedId, partLabel } from '../chunkedSpeakers';

describe('parseChunkedId', () => {
  it('extracts the chunk index and the raw id', () => {
    expect(parseChunkedId('c0:SPEAKER_00')).toEqual({ chunkIndex: 0, rawId: 'SPEAKER_00' });
    expect(parseChunkedId('c2:SPEAKER_01')).toEqual({ chunkIndex: 2, rawId: 'SPEAKER_01' });
  });

  it('handles multi-digit chunk indices', () => {
    expect(parseChunkedId('c11:SPEAKER_00')).toEqual({ chunkIndex: 11, rawId: 'SPEAKER_00' });
  });

  it('returns null for an ordinary, unchunked id', () => {
    expect(parseChunkedId('SPEAKER_00')).toBeNull();
  });

  it('returns null for a display name someone happened to type', () => {
    // A user renaming a speaker "Case 1: intro" must not look chunked.
    expect(parseChunkedId('Case 1: intro')).toBeNull();
  });

  it('returns null for an empty string', () => {
    expect(parseChunkedId('')).toBeNull();
  });
});

describe('partLabel', () => {
  it('is 1-based for display even though the chunk index is 0-based', () => {
    expect(partLabel('c0:SPEAKER_00')).toBe('Part 1');
    expect(partLabel('c1:SPEAKER_00')).toBe('Part 2');
  });

  it('is null for an ordinary id', () => {
    expect(partLabel('SPEAKER_00')).toBeNull();
  });
});
