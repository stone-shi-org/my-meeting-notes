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
}: {
  streams: LiveStreams;
  enabled: boolean;
}) {
  const { captions, connected } = useLiveCaption(streams, enabled);

  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface-2/50 p-3">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium text-fg-subtle">Live captions</p>
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
      {captions.length === 0 ? (
        <p className="text-sm text-fg-faint">Waiting for speech…</p>
      ) : (
        <ul className="max-h-40 space-y-1 overflow-y-auto text-sm">
          {captions.map((c, i) => (
            <li key={`${c.at}-${c.channel}-${i}`} className="text-fg-muted">
              <span className="font-medium text-fg">{CHANNEL_LABEL[c.channel]}:</span> {c.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
