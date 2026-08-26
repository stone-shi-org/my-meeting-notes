import { useEffect, useMemo, useRef, useState } from 'react';
import { Zap } from 'lucide-react';
import { Badge, Card, Input } from '@/components/ui/primitives';
import {
  LIVE_CAPTION_BACKEND_BADGE,
  type Caption,
  type CaptionWarning,
  type LiveCaptionBackend,
} from '@/hooks/useLiveCaption';
import { cn } from '@/lib/cn';
import { initials, speakerVars } from '@/lib/speakerColors';

const DEFAULT_LABELS: Record<'me' | 'room', string> = { me: 'You', room: 'Room' };
const AUTO_SCROLL_KEY = 'mmn.liveTranscriptAutoScroll';

/** How fast a freshly-arrived chunk types itself out. Each ASR call commits
 * a whole window's text at once (see live_caption.py) -- there is no real
 * per-word stream to relay -- so this is a cosmetic reveal on top of an
 * already-final string, tuned to comfortably finish before the next window
 * lands (interval_sec defaults to 3s) rather than to match true speaking
 * pace, which would still be catching up when the next chunk arrives. */
const STREAM_MS_PER_WORD = 70;

/**
 * One already-final caption, revealed word by word on mount instead of all
 * at once -- Meet-style "streaming" captions, faked on top of chunky
 * delivery. `text` is immutable for the lifetime of one Caption (see
 * useLiveCaption -- captions are only ever appended, never edited), so this
 * only ever animates once, right when its <li> first mounts; a caption that
 * scrolled by earlier keeps whatever it already revealed; no replay.
 */
function StreamedText({ text }: { text: string }) {
  const words = useMemo(() => text.split(/\s+/).filter(Boolean), [text]);
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (words.length === 0) return;
    let n = 0;
    const id = window.setInterval(() => {
      n += 1;
      setCount(n);
      if (n >= words.length) window.clearInterval(id);
    }, STREAM_MS_PER_WORD);
    return () => window.clearInterval(id);
    // words is a fresh array each render, but its *contents* are fixed for
    // the lifetime of this text -- re-running on identity would restart the
    // reveal on every unrelated re-render of the parent list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <>{words.slice(0, count).join(' ')}</>;
}

interface CaptionGroup {
  channel: 'me' | 'room';
  parts: Caption[];
}

/**
 * Consecutive same-channel captions collapse into one conversational block
 * instead of one line per ASR call -- a channel that just keeps talking
 * produces a new caption every interval_sec, and repeating its name on
 * every single one read as a wall of "Room: ... / Room: ... / Room: ..."
 * rather than the one continuous thing actually being said. A channel
 * *switch* still starts a new group -- that boundary is the one that means
 * something (an actual turn, not a polling artifact).
 */
function groupCaptions(captions: Caption[]): CaptionGroup[] {
  const groups: CaptionGroup[] = [];
  for (const c of captions) {
    const last = groups[groups.length - 1];
    if (last && last.channel === c.channel) last.parts.push(c);
    else groups.push({ channel: c.channel, parts: [c] });
  }
  return groups;
}

/**
 * The bigger panel of the wide recorder layout (right column, next to the
 * controls -- see RecorderPanel): a disposable, auto-scrolling draft of
 * what's being said, laid out like a chat -- "you" on the right, the other
 * side on the left -- with room to also rename the two channels while
 * you're on the call.
 *
 * Takes captions rather than opening its own useLiveCaption connection --
 * RecorderPanel owns the one websocket and hands the same array to
 * InsightsPanel below this, so the two don't each pay for a live-caption
 * connection (and the periodic real transcription call behind it) of their
 * own for what is, on the wire, identical audio.
 *
 * The rename is local-only, like everything else here -- there is no
 * meeting or transcript row yet to attach a name to, and even once one
 * exists these channel tags ("room"/"me") aren't the diarized speaker ids
 * the real transcript ends up with. It only relabels what's on screen for
 * the rest of this recording -- Insights below always sees the raw
 * "room"/"me" tags, matching what its prompt is told to expect.
 */
export function LiveTranscriptPanel({
  captions,
  connected,
  enabled,
  backend,
  partial,
  warnings,
  soleChannel,
}: {
  captions: Caption[];
  connected: boolean;
  enabled: boolean;
  backend?: LiveCaptionBackend | null;
  /** In-progress text for a still-open utterance on each channel -- see
   * useLiveCaption's `partial`. Optional so callers that don't wire it
   * through (none currently, but nothing here requires it) still compile;
   * an absent value just means no preview bubble is shown. */
  partial?: Record<'me' | 'room', string>;
  /** Channel-level failures -- see useLiveCaption's CaptionWarning. Optional
   * for the same reason `partial` is: an absent value just means nothing is
   * shown, not that nothing went wrong. */
  warnings?: CaptionWarning[];
  /** Set when the capture only ever has one audio source -- a plain mic
   * recording (`'me'`), or a tab/system share with no mic mixed in
   * (`'room'`) -- see useRecorder's `liveStreams`/`mixing`. `null` while two
   * sources are genuinely being kept apart (a real "you" vs "room" split) or
   * before a recording has started. A lone phone mic sitting on a table can
   * just as easily be picking up a whole room, so `'me'` here suppresses the
   * "You" framing below rather than claim an attribution the capture has no
   * way to back up. */
  soleChannel?: 'me' | 'room' | null;
}) {
  const [labels, setLabels] = useState<Record<'me' | 'room', string>>(DEFAULT_LABELS);
  const soleMicOnly = soleChannel === 'me';
  const defaultLabelFor = (channel: 'me' | 'room') =>
    soleMicOnly && channel === 'me' ? 'Mic' : DEFAULT_LABELS[channel];
  const [autoScroll, setAutoScroll] = useState(() => localStorage.getItem(AUTO_SCROLL_KEY) !== '0');
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => groupCaptions(captions), [captions]);

  useEffect(() => {
    localStorage.setItem(AUTO_SCROLL_KEY, autoScroll ? '1' : '0');
  }, [autoScroll]);

  // Watches the *content*'s height, not the captions array: a new caption
  // arriving is one height change, but StreamedText growing an existing
  // bubble word by word (see above) is a run of further ones with no new
  // caption behind them. Re-running only on `captions` (the original
  // version) stuck to the bottom for whichever bubble had just arrived,
  // then never again -- every word that bubble typed out afterward grew
  // the page below the fold, so the newest text stayed permanently hidden
  // until the *next* caption happened to drag the scroll back down.
  useEffect(() => {
    const container = scrollRef.current;
    const content = contentRef.current;
    if (!autoScroll || !container || !content) return;

    const stickToBottom = () => {
      container.scrollTop = container.scrollHeight;
    };
    stickToBottom();

    const observer = new ResizeObserver(stickToBottom);
    observer.observe(content);
    return () => observer.disconnect();
  }, [autoScroll]);

  const labelFor = (channel: 'me' | 'room') => labels[channel].trim() || defaultLabelFor(channel);

  const avatarFor = (channel: 'me' | 'room') => (
    <span
      aria-hidden
      className="grid size-6 shrink-0 place-items-center rounded-full text-[10px] font-semibold"
      style={{
        ...speakerVars(channel),
        backgroundColor: 'color-mix(in srgb, var(--sp) 18%, transparent)',
        color: 'var(--sp-ink)',
      }}
    >
      {initials(labelFor(channel))}
    </span>
  );

  // Channels with an in-progress preview -- see useLiveCaption's `partial`.
  // Rendered as their own (at most two) bubbles after the committed groups,
  // never merged into them: a partial is not yet a Caption (no `at`, no
  // stable identity) and is about to be replaced wholesale by the next
  // `caption` message, unlike StreamedText's reveal-in-place of an already
  // final string.
  const partialChannels = partial
    ? (['me', 'room'] as const).filter((ch) => partial[ch])
    : [];

  return (
    <Card className="flex h-[680px] max-h-[78vh] flex-col overflow-hidden p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h2 className="font-display text-lg font-semibold">Live transcript</h2>
          {backend && (
            <span
              className="inline-flex items-center gap-1 rounded bg-indigo-500/10 px-2 py-0.5 text-xs font-medium text-indigo-400 border border-indigo-500/20"
              title={LIVE_CAPTION_BACKEND_BADGE[backend].title}
            >
              <Zap className="size-3" />
              {LIVE_CAPTION_BACKEND_BADGE[backend].label}
            </span>
          )}
        </div>
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
        {soleChannel &&
          ' One audio source right now, so this preview can’t split speakers apart either.'}
      </p>

      {/* A channel-level failure (see useLiveCaption's CaptionWarning) --
          the only way something like a rejected live_caption_language is
          visible at all, instead of that channel just sitting on "Waiting
          for speech…" forever with nothing to explain why. */}
      {warnings && warnings.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-danger-ink" role="alert">
          {warnings.map((w, i) => (
            <li key={`${w.at}-${i}`}>
              <span className="font-medium">{labelFor(w.channel)}:</span> {w.message}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-4 rounded-lg border border-border bg-surface-2/50 p-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
          {soleChannel ? 'Label' : 'Speakers'}
        </h3>
        <ul className="space-y-2">
          {(soleChannel ? [soleChannel] : (['me', 'room'] as const)).map((channel) => (
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
                placeholder={defaultLabelFor(channel)}
                aria-label={`Name for ${defaultLabelFor(channel)}`}
                onChange={(e) =>
                  setLabels((prev) => ({ ...prev, [channel]: e.target.value }))
                }
              />
              {channel === 'me' && !soleMicOnly && (
                <Badge variant="primary" size="sm">
                  You
                </Badge>
              )}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-fg-subtle">
          Renaming only changes the label{soleChannel ? '' : 's'} above and in the transcript
          below — nothing is saved, and it has no effect on the real transcript built after you
          stop.
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
        <div ref={contentRef}>
          {captions.length === 0 && partialChannels.length === 0 ? (
            <p className="text-sm text-fg-faint">
              {enabled
                ? 'Waiting for speech…'
                : 'Turn on "Show live captions" and start recording to see a running transcript here.'}
            </p>
          ) : (
            <ul className="space-y-3 text-sm">
              {groups.map((g, gi) => {
                const isMe = g.channel === 'me' && !soleMicOnly;
                return (
                  <li
                    key={`${g.parts[0].at}-${g.channel}-${gi}`}
                    className={cn('flex items-end gap-2', isMe ? 'flex-row-reverse' : 'flex-row')}
                  >
                    {avatarFor(g.channel)}
                    <div
                      className={cn(
                        'max-w-[85%] rounded-2xl px-3 py-2',
                        isMe ? 'bg-primary-soft text-primary-soft-fg' : 'bg-surface-2',
                      )}
                    >
                      <p
                        className={cn(
                          'text-xs font-medium',
                          isMe ? 'text-primary-soft-fg/80' : 'text-fg-subtle',
                        )}
                      >
                        {labelFor(g.channel)}
                      </p>
                      <p className={cn('mt-0.5', isMe ? '' : 'text-fg-muted')}>
                        {g.parts.map((p, pi) => (
                          <span key={`${p.at}-${pi}`}>
                            {pi > 0 && ' '}
                            <StreamedText text={p.text} />
                          </span>
                        ))}
                      </p>
                    </div>
                  </li>
                );
              })}
              {/* In-progress bubbles for an utterance that hasn't committed
                  yet -- faded/italic so they read as provisional, distinct
                  from every settled bubble above. Relayed if the backend
                  ever emits a transcription delta mid-utterance (not
                  observed against the current /v1/realtime deployment, but
                  wired up for when it does), so a caption can show up
                  before the server's own VAD decides the utterance is
                  over and a `caption` message commits. */}
              {partialChannels.map((ch) => {
                const isMe = ch === 'me' && !soleMicOnly;
                return (
                  <li
                    key={`partial-${ch}`}
                    className={cn(
                      'flex items-end gap-2 opacity-70',
                      isMe ? 'flex-row-reverse' : 'flex-row',
                    )}
                  >
                    {avatarFor(ch)}
                    <div
                      className={cn(
                        'max-w-[85%] rounded-2xl px-3 py-2',
                        isMe ? 'bg-primary-soft text-primary-soft-fg' : 'bg-surface-2',
                      )}
                    >
                      <p
                        className={cn(
                          'text-xs font-medium',
                          isMe ? 'text-primary-soft-fg/80' : 'text-fg-subtle',
                        )}
                      >
                        {labelFor(ch)}
                      </p>
                      <p className={cn('mt-0.5 italic', isMe ? '' : 'text-fg-muted')}>
                        {partial![ch]}…
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}
