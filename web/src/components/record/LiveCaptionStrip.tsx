import { Zap } from 'lucide-react';
import { useLiveCaption } from '@/hooks/useLiveCaption';
import type { LiveStreams } from '@/hooks/useRecorder';
import { cn } from '@/lib/cn';

const CHANNEL_LABEL: Record<'me' | 'room', string> = {
  me: 'You',
  room: 'Room',
};

/**
 * A rough, disposable transcript strip shown while a recording is in
 * progress. Never the source of the real transcript -- that is still built
 * from the finished audio after Stop, same as always -- this exists purely
 * so there is something to read during the meeting.
 */
export function LiveCaptionStrip({
  streams,
  enabled,
  language,
}: {
  streams: LiveStreams;
  enabled: boolean;
  /** ISO-639-1 code, or '' for auto-detect -- see useLiveCaption. */
  language: string;
}) {
  const { captions, connected, isRealtime, partial } = useLiveCaption(streams, enabled, language);
  // Channels with an in-progress preview worth a row of their own -- order
  // doesn't matter here (there are at most two), unlike the committed list
  // below which is append-only and already in arrival order.
  const partialChannels = (['me', 'room'] as const).filter((ch) => partial[ch]);

  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface-2/50 p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <p className="text-xs font-medium text-fg-subtle">Live captions</p>
          {isRealtime && (
            <span
              className="inline-flex items-center gap-1 rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] font-medium text-indigo-400 border border-indigo-500/20"
              title="Streaming via a persistent /v1/realtime session"
            >
              <Zap className="size-2.5" />
              Realtime
            </span>
          )}
        </div>
        <span
          className={cn(
            'inline-flex items-center gap-1.5 text-xs',
            connected ? 'text-success-ink' : 'text-fg-faint',
          )}
        >
          <span
            className={cn('size-1.5 rounded-full', connected ? 'bg-success' : 'bg-fg-faint')}
            aria-hidden
          />
          {connected ? 'Live' : 'Connecting…'}
        </span>
      </div>
      {captions.length === 0 && partialChannels.length === 0 ? (
        <p className="text-sm text-fg-faint">Waiting for speech…</p>
      ) : (
        <ul className="max-h-40 space-y-1 overflow-y-auto text-sm">
          {captions.map((c, i) => (
            <li key={`${c.at}-${c.channel}-${i}`} className="text-fg-muted">
              <span className="font-medium text-fg">{CHANNEL_LABEL[c.channel]}:</span> {c.text}
            </li>
          ))}
          {/* In-progress text for a call that hasn't committed yet -- see
              useLiveCaption's `partial`. Muted/italic so it visually reads
              as provisional, and never confused with a committed caption
              above it: this row's key is stable per channel and gets
              replaced in place as new deltas arrive, not appended. */}
          {partialChannels.map((ch) => (
            <li key={`partial-${ch}`} className="italic text-fg-faint">
              <span className="font-medium">{CHANNEL_LABEL[ch]}:</span> {partial[ch]}…
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
