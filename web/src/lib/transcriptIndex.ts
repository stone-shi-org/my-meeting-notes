/**
 * Active-segment lookup for the audio player.
 *
 * Called ~10x/second while playing, so the common case -- the playhead is still
 * inside the current segment, or has just stepped into the next one -- is a
 * couple of comparisons. Only a seek falls through to the binary search.
 */

export interface SegmentTimes {
  start: number;
  end: number;
}

export interface TranscriptIndex {
  starts: Float64Array;
  ends: Float64Array;
}

export function buildIndex(segments: SegmentTimes[]): TranscriptIndex {
  const starts = new Float64Array(segments.length);
  const ends = new Float64Array(segments.length);
  for (let i = 0; i < segments.length; i++) {
    starts[i] = segments[i].start;
    ends[i] = segments[i].end;
  }
  return { starts, ends };
}

/**
 * Index of the segment covering `t`, or the last segment that started before it.
 *
 * Segments have real gaps between them; in a gap we hold the previous segment
 * rather than returning -1, so the highlight doesn't flicker off between turns.
 * Returns -1 only when `t` precedes the first segment.
 */
export function findIndex(idx: TranscriptIndex, t: number, hint = -1): number {
  const n = idx.starts.length;
  if (n === 0) return -1;

  if (hint >= 0 && hint < n) {
    if (t >= idx.starts[hint] && t < idx.ends[hint]) return hint;
    const next = hint + 1;
    if (next < n && t >= idx.starts[next] && t < idx.ends[next]) return next;
    // In the gap after `hint` and before the next segment starts.
    if (t >= idx.ends[hint] && (next >= n || t < idx.starts[next])) return hint;
  }

  let lo = 0;
  let hi = n - 1;
  let res = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (idx.starts[mid] <= t) {
      res = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return res;
}
