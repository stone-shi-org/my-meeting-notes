/**
 * Recognising a chunked diarization's speaker ids on the frontend.
 *
 * pipeline._stitch_chunk_payloads namespaces every speaker/segment id by
 * chunk index -- "c0:SPEAKER_00", "c1:SPEAKER_00" -- because each chunk gets
 * fresh SPEAKER_nn numbering from the model with no memory of the chunk
 * before it. The same physical person can end up under a different raw id
 * in every chunk, and nothing links them back together automatically: that
 * reconciliation is the existing "merge speakers" move, done once per
 * chunk-boundary mismatch.
 *
 * A speaker's own `id` keeps its chunk prefix forever, rename or not --
 * renaming only ever touches display_name (see transcript.py's
 * display_name_for). That's what makes it safe to parse here for a
 * persistent "Part N" badge instead of relying on the Speaker Legend's
 * placeholder text, which disappears the moment a name is typed in.
 */

const CHUNK_ID_RE = /^c(\d+):(.+)$/;

export interface ChunkedId {
  /** Zero-based chunk index, as assigned by _stitch_chunk_payloads. */
  chunkIndex: number;
  /** The id with its "cN:" prefix removed -- e.g. "SPEAKER_00". */
  rawId: string;
}

export function parseChunkedId(id: string): ChunkedId | null {
  const match = CHUNK_ID_RE.exec(id);
  if (!match) return null;
  return { chunkIndex: Number(match[1]), rawId: match[2] };
}

/** "Part 2" for a chunk-namespaced id, or null for an ordinary one. Parts are
 * 1-based for display -- the chunk index underneath is 0-based. */
export function partLabel(id: string): string | null {
  const parsed = parseChunkedId(id);
  return parsed ? `Part ${parsed.chunkIndex + 1}` : null;
}
