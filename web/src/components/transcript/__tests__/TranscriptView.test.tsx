import { describe, expect, it } from 'vitest';
import { computeChunkDividers } from '../TranscriptView';
import type { Segment } from '@/types/api';

function seg(id: number, start: number): Segment {
  return {
    id,
    speaker: 'SPEAKER_00',
    speaker_name: 'SPEAKER_00',
    label: null,
    start,
    end: start + 1,
    text: `segment ${id}`,
    non_speech: false,
    is_me: false,
  };
}

describe('computeChunkDividers', () => {
  it('marks nothing for an unchunked transcript (no or one boundary)', () => {
    const segments = [seg(0, 0), seg(1, 10)];
    expect(computeChunkDividers(segments, []).size).toBe(0);
    expect(computeChunkDividers(segments, [0]).size).toBe(0);
  });

  it('places a divider at the first segment crossing into a later part', () => {
    const segments = [seg(0, 0), seg(1, 10), seg(2, 1500), seg(3, 1510)];
    const dividers = computeChunkDividers(segments, [0, 1500]);

    expect(dividers.size).toBe(1);
    expect(dividers.get(2)).toEqual({ part: 2, start: 1500 });
    // Not before it -- the divider marks where part 2 starts, not before part 1 ends.
    expect(dividers.has(1)).toBe(false);
  });

  it('never marks a divider at the very first segment for chunk 1 itself', () => {
    const segments = [seg(0, 0), seg(1, 10)];
    const dividers = computeChunkDividers(segments, [0, 1500, 3000]);
    // Nothing here ever reaches 1500 or 3000.
    expect(dividers.size).toBe(0);
  });

  it('handles three or more parts', () => {
    const segments = [seg(0, 0), seg(1, 1600), seg(2, 3100)];
    const dividers = computeChunkDividers(segments, [0, 1500, 3000]);

    expect(dividers.get(1)).toEqual({ part: 2, start: 1500 });
    expect(dividers.get(2)).toEqual({ part: 3, start: 3000 });
  });

  it('re-indexes against a filtered (searched) segment list, not the original', () => {
    // Simulates the caller passing the *filtered* array -- indices here are
    // positions within it, since that's what the render loop iterates over.
    const filtered = [seg(0, 0), seg(2, 1500)]; // segment 1 filtered out
    const dividers = computeChunkDividers(filtered, [0, 1500]);
    expect(dividers.get(1)).toEqual({ part: 2, start: 1500 });
  });

  it('attributes a segment exactly on the boundary to the new part', () => {
    const segments = [seg(0, 1499.999), seg(1, 1500)];
    const dividers = computeChunkDividers(segments, [0, 1500]);
    expect(dividers.has(0)).toBe(false);
    expect(dividers.get(1)).toEqual({ part: 2, start: 1500 });
  });
});
