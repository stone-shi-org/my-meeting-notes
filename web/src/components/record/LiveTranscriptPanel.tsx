import { useEffect, useRef, useState } from 'react';
import { Badge, Card, Input } from '@/components/ui/primitives';
import { useLiveCaption } from '@/hooks/useLiveCaption';
import type { LiveStreams } from '@/hooks/useRecorder';
import { cn } from '@/lib/cn';
import { initials, speakerVars } from '@/lib/speakerColors';

const DEFAULT_LABELS: Record<'me' | 'room', string> = { me: 'You', room: 'Room' };
const AUTO_SCROLL_KEY = 'mmn.liveTranscriptAutoScroll';

/**
 * The bigger, left-hand panel of the wide recorder layout: a disposable,
 * auto-scrolling draft of what's being said, built the same way
 * LiveCaptionStrip is (see useLiveCaption) but with room to also rename the
 * two channels while you're on the call.
 *
 * The rename is local-only, like everything else here -- there is no
 * meeting or transcript row yet to attach a name to, and even once one
 * exists these channel tags ("room"/"me") aren't the diarized speaker ids
 * the real transcript ends up with. It only relabels what's on screen for
 * the rest of this recording.
 */
export function LiveTranscriptPanel({
  streams,
  enabled,
}: {
  streams: LiveStreams;
  enabled: boolean;
}) {
  const { captions, connected } = useLiveCaption(streams, enabled);
  const [labels, setLabels] = useState<Record<'me' | 'room', string>>(DEFAULT_LABELS);
  const [autoScroll, setAutoScroll] = useState(() => localStorage.getItem(AUTO_SCROLL_KEY) !== '0');
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem(AUTO_SCROLL_KEY, autoScroll ? '1' : '0');
  }, [autoScroll]);

  useEffect(() => {
    if (!autoScroll) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [captions, autoScroll]);

  const labelFor = (channel: 'me' | 'room') => labels[channel].trim() || DEFAULT_LABELS[channel];

  return (
    <Card className="flex h-[680px] max-h-[78vh] flex-col overflow-hidden p-5">
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-display text-lg font-semibold">Live transcript</h2>
        <span
          className={cn(
            'inline-flex shrink-0 items-center gap-1.5 text-xs',
            connected ? 'text-success-ink' : 'text-fg-faint',
          )}
        >
          <span
            className={cn('size-1.5 rounded-full', connected ? 'bg-success' : 'bg-fg-faint')}
            aria-hidden
          />
          {!enabled ? 'Off' : connected ? 'Live' : 'Connecting…'}
        </span>
      </div>
      <p className="mt-1 text-xs text-fg-subtle">
        A rough draft only — the real, speaker-labelled transcript is still built after you stop.
      </p>

      <div className="mt-4 rounded-lg border border-border bg-surface-2/50 p-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
          Speakers
        </h3>
        <ul className="space-y-2">
          {(['me', 'room'] as const).map((channel) => (
            <li
              key={channel}
              className="flex items-center gap-2"
              style={speakerVars(channel)}
            >
              <span
                aria-hidden
                className="grid size-6 shrink-0 place-items-center rounded-full text-[10px] font-semibold"
                style={{
                  backgroundColor: 'color-mix(in srgb, var(--sp) 18%, transparent)',
                  color: 'var(--sp-ink)',
                }}
              >
                {initials(labelFor(channel))}
              </span>
              <Input
                className="h-8 flex-1 border-transparent bg-transparent px-2 hover:border-border-strong focus:border-border-strong"
                value={labels[channel]}
                placeholder={DEFAULT_LABELS[channel]}
                aria-label={`Name for ${DEFAULT_LABELS[channel]}`}
                onChange={(e) =>
                  setLabels((prev) => ({ ...prev, [channel]: e.target.value }))
                }
              />
              {channel === 'me' && (
                <Badge variant="primary" size="sm">
                  You
                </Badge>
              )}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-fg-subtle">
          Renaming only changes the labels above and in the transcript below — nothing is saved,
          and it has no effect on the real transcript built after you stop.
        </p>
      </div>

      <label className="mt-3 flex items-center gap-2 text-xs text-fg-muted">
        <input
          type="checkbox"
          checked={autoScroll}
          onChange={(e) => setAutoScroll(e.target.checked)}
          className="size-3.5 rounded border-border-strong"
        />
        Auto-scroll
      </label>

      <div ref={scrollRef} className="mt-2 min-h-0 flex-1 overflow-y-auto pr-1">
        {captions.length === 0 ? (
          <p className="text-sm text-fg-faint">
            {enabled
              ? 'Waiting for speech…'
              : 'Turn on "Show live captions" and start recording to see a running transcript here.'}
          </p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {captions.map((c, i) => (
              <li key={`${c.at}-${c.channel}-${i}`} style={speakerVars(c.channel)}>
                <span className="font-medium" style={{ color: 'var(--sp-ink)' }}>
                  {labelFor(c.channel)}:
                </span>{' '}
                <span className="text-fg-muted">{c.text}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
