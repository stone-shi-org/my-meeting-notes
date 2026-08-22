import { Link2, Pencil } from 'lucide-react';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Badge } from '@/components/ui/primitives';
import { cn } from '@/lib/cn';
import { initials, speakerVars } from '@/lib/speakerColors';
import { fmtClock } from '@/lib/time';
import { usePlayer } from '@/player/PlayerProvider';
import { useIsActive, usePlayerStore } from '@/player/playerStore';
import type { Segment } from '@/types/api';

interface ChunkDivider {
  /** 1-based, for display -- the chunk index underneath is 0-based. */
  part: number;
  start: number;
}

/** Where to draw a "Part N starts here" divider among an (already
 * time-ordered, possibly search-filtered) list of segments.
 *
 * Keyed by index into `segments` rather than by segment id: a divider marks
 * a position in the rendered list, not a property of any one segment.
 * `chunkBoundaries[0]` is always the recording's own start (0), which needs
 * no divider -- only crossings *into* a later part do.
 */
export function computeChunkDividers(
  segments: Segment[],
  chunkBoundaries: number[],
): Map<number, ChunkDivider> {
  const dividers = new Map<number, ChunkDivider>();
  if (chunkBoundaries.length < 2) return dividers;

  let boundaryIdx = 1;
  for (let i = 0; i < segments.length; i++) {
    while (
      boundaryIdx < chunkBoundaries.length &&
      segments[i].start >= chunkBoundaries[boundaryIdx]
    ) {
      dividers.set(i, { part: boundaryIdx + 1, start: chunkBoundaries[boundaryIdx] });
      boundaryIdx++;
    }
  }
  return dividers;
}

function ChunkDividerRow({ divider, onSeek }: { divider: ChunkDivider; onSeek: (t: number) => void }) {
  return (
    <li className="flex items-center gap-2 bg-surface-2/60 px-4 py-1.5" aria-hidden={false}>
      <span className="h-px flex-1 bg-border" aria-hidden />
      <button
        onClick={() => onSeek(divider.start)}
        className="shrink-0 whitespace-nowrap text-xs font-medium text-fg-subtle hover:text-primary"
        title="This recording was long enough to be diarized in pieces -- speakers may need renaming or merging again from here"
      >
        Part {divider.part} starts here · {fmtClock(divider.start)}
      </button>
      <span className="h-px flex-1 bg-border" aria-hidden />
    </li>
  );
}

export function SpeakerChip({
  speakerId,
  name,
  onRename,
  size = 'md',
  isMe = false,
}: {
  speakerId: string;
  name: string;
  onRename?: () => void;
  size?: 'sm' | 'md';
  isMe?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-1.5" style={speakerVars(speakerId)}>
      {/* The mark is never rendered without the name beside it: colour is a
          redundant accelerator, not the carrier of identity. */}
      <span
        aria-hidden
        className={cn(
          'grid place-items-center rounded-full font-semibold',
          size === 'sm' ? 'size-5 text-[9px]' : 'size-6 text-[10px]',
        )}
        style={{
          backgroundColor: 'color-mix(in srgb, var(--sp) 18%, transparent)',
          color: 'var(--sp-ink)',
        }}
      >
        {initials(name)}
      </span>
      <span
        className={cn('font-medium', size === 'sm' ? 'text-xs' : 'text-sm')}
        style={{ color: 'var(--sp-ink)' }}
      >
        {name}
      </span>
      {isMe && (
        <Badge variant="primary" size="sm">
          You
        </Badge>
      )}
      {onRename && (
        <button
          onClick={onRename}
          aria-label={`Rename ${name}`}
          className="rounded p-0.5 text-fg-faint opacity-0 transition-opacity group-hover:opacity-100 hover:text-fg focus-visible:opacity-100"
        >
          <Pencil className="size-3" />
        </button>
      )}
    </span>
  );
}

const SegmentRow = memo(function SegmentRow({
  segment,
  index,
  meetingId,
  onRename,
}: {
  segment: Segment;
  index: number;
  meetingId: number;
  onRename: (speakerId: string) => void;
}) {
  const active = useIsActive(index);
  const { seek } = usePlayer();
  const ref = useRef<HTMLLIElement>(null);
  const follow = usePlayerStore((s) => s.follow);

  useEffect(() => {
    if (!active || !follow) return;
    ref.current?.scrollIntoView({
      block: 'center',
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
        ? 'auto'
        : 'smooth',
    });
  }, [active, follow]);

  if (segment.non_speech) {
    return (
      <li
        ref={ref}
        className="px-4 py-1.5 text-sm italic text-fg-faint opacity-60"
        aria-current={active ? 'location' : undefined}
      >
        <span className="font-mono text-xs">{fmtClock(segment.start)}</span> {segment.text}
      </li>
    );
  }

  return (
    <li
      ref={ref}
      aria-current={active ? 'location' : undefined}
      className={cn(
        'group relative border-l-[3px] px-4 py-3 transition-colors duration-fast',
        active
          ? 'border-l-[color:var(--sp)] bg-primary-soft/40'
          : 'border-l-transparent hover:bg-surface-2/60',
      )}
      style={speakerVars(segment.speaker)}
    >
      <div className="mb-1 flex items-center gap-3">
        <SpeakerChip
          speakerId={segment.speaker}
          name={segment.speaker_name}
          onRename={() => onRename(segment.speaker)}
          isMe={segment.is_me}
        />
        <button
          onClick={() => seek(segment.start)}
          className="font-mono text-xs text-fg-faint hover:text-primary"
          aria-label={`Seek to ${fmtClock(segment.start)}`}
        >
          {fmtClock(segment.start)}
        </button>
        <button
          onClick={() => {
            void navigator.clipboard.writeText(
              `${window.location.origin}/meetings/${meetingId}?t=${Math.floor(segment.start)}`,
            );
          }}
          aria-label="Copy a link to this moment"
          className="rounded p-0.5 text-fg-faint opacity-0 transition-opacity group-hover:opacity-100 hover:text-primary focus-visible:opacity-100"
        >
          <Link2 className="size-3" />
        </button>
      </div>

      <button
        onClick={() => seek(segment.start, true)}
        className="block w-full text-left text-md leading-relaxed text-fg"
      >
        {segment.text}
      </button>
    </li>
  );
});

export function TranscriptView({
  segments,
  meetingId,
  onRename,
  chunkBoundaries = [],
}: {
  segments: Segment[];
  meetingId: number;
  onRename: (speakerId: string) => void;
  /** Start offsets of each piece, when this meeting was long enough to be
   * diarized in chunks (see pipeline._diarize_in_chunks). Draws a "Part N
   * starts here" divider at each crossing. */
  chunkBoundaries?: number[];
}) {
  const [query, setQuery] = useState('');
  const { seek } = usePlayer();

  const filtered = query
    ? segments.filter((s) => s.text.toLowerCase().includes(query.toLowerCase()))
    : segments;

  const dividers = useMemo(
    () => computeChunkDividers(filtered, chunkBoundaries),
    [filtered, chunkBoundaries],
  );

  return (
    <div>
      <div className="sticky top-0 z-20 border-b border-border bg-surface/90 px-3 py-2 backdrop-blur-xl">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Find in transcript…"
          aria-label="Find in transcript"
          className="h-8 w-full rounded border border-border-strong bg-surface px-3 text-sm"
        />
        {query && (
          <p className="mt-1 text-xs text-fg-subtle">
            {filtered.length} of {segments.length} segments
          </p>
        )}
      </div>

      <ol role="list">
        {filtered.flatMap((segment, i) => {
          // Index into the *unfiltered* list: the player's active index refers
          // to real positions, not filtered ones.
          const realIndex = segments.indexOf(segment);
          const divider = dividers.get(i);
          const row = (
            <SegmentRow
              key={`${segment.id}-${segment.start}`}
              segment={segment}
              index={realIndex}
              meetingId={meetingId}
              onRename={onRename}
            />
          );
          return divider
            ? [<ChunkDividerRow key={`divider-${divider.part}`} divider={divider} onSeek={seek} />, row]
            : [row];
        })}
      </ol>
    </div>
  );
}
