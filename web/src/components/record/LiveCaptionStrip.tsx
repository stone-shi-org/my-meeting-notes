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
  const { captions, connected, isCacheAware } = useLiveCaption(streams, enabled, language);

  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface-2/50 p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <p className="text-xs font-medium text-fg-subtle">Live captions</p>
          {isCacheAware && (
            <span
              className="inline-flex items-center gap-1 rounded bg-indigo-500/10 px-1.5 py-0.5 text-[10px] font-medium text-indigo-400 border border-indigo-500/20"
              title="Native Cache-Aware Streaming RNN-T Enabled"
            >
              <Zap className="size-2.5" />
              Cache-Aware
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
