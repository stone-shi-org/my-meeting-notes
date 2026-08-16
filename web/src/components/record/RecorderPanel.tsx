import {
  AppWindow,
  Circle,
  Mic,
  Monitor,
  Pause,
  Play,
  RefreshCw,
  Square,
  Trash2,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Label, Select } from '@/components/ui/primitives';
import { InsightsPanel } from '@/components/record/InsightsPanel';
import { LiveCaptionStrip } from '@/components/record/LiveCaptionStrip';
import { LiveTranscriptPanel } from '@/components/record/LiveTranscriptPanel';
import { useLiveCaption } from '@/hooks/useLiveCaption';
import { type ChannelMap, useAudioInputs, useRecorder } from '@/hooks/useRecorder';
import { cn } from '@/lib/cn';
import {
  blockedReason,
  detectPlatform,
  fmtElapsedMs,
  sourceSupport,
  type Source,
} from '@/lib/recording';

export type RoomSpeakers = 'single' | 'multiple';

const SOURCES: { id: Source; label: string; icon: typeof Mic }[] = [
  { id: 'mic', label: 'Microphone', icon: Mic },
  { id: 'tab', label: 'Browser tab', icon: AppWindow },
  { id: 'system', label: 'System audio', icon: Monitor },
];

/**
 * A level meter, and the reason this panel has one.
 *
 * A capture that is running but silent looks exactly like a capture that is
 * working, and the way to end up with one is to miss Chrome's "Also share tab
 * audio" checkbox. Twenty bars that never move say so in the first two seconds
 * rather than after the meeting.
 */
function LevelMeter({ level, active }: { level: number; active: boolean }) {
  const bars = 20;
  const lit = Math.round(level * bars);
  return (
    <div
      className="flex items-center gap-[3px]"
      role="meter"
      aria-valuenow={Math.round(level * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="Input level"
    >
      {Array.from({ length: bars }).map((_, i) => (
        <span
          key={i}
          className={cn(
            'h-4 w-1 rounded-full transition-colors duration-fast',
            !active || i >= lit
              ? 'bg-surface-3'
              : i > bars - 3
                ? 'bg-danger'
                : i > bars - 6
                  ? 'bg-warning'
                  : 'bg-success',
          )}
        />
      ))}
    </div>
  );
}

export function RecorderPanel({
  onRecorded,
  disabled,
  layout = 'compact',
  rightExtra,
  onLiveChange,
}: {
  /** Handed the finished clip as a File, ready for the normal upload path,
   * plus whether it kept mic/room on separate channels and -- only
   * meaningful alongside that -- how many people were on the room side. */
  onRecorded: (
    file: File | null,
    durationSec: number,
    channelMap: ChannelMap,
    roomSpeakers: RoomSpeakers,
  ) => void;
  disabled?: boolean;
  /** 'wide' puts a bigger, always-mounted LiveTranscriptPanel on the left and
   * shrinks these controls to a right-hand column -- see NewMeetingPage,
   * which is the only caller with room to spare. Everywhere else (adding a
   * recording to an existing meeting, which shares a narrower column with
   * the rest of that page) stays 'compact': the inline LiveCaptionStrip
   * beneath the controls, same as before this existed. */
  layout?: 'compact' | 'wide';
  /** Only rendered in 'wide' layout, stacked below the controls in the same
   * right-hand column -- see AudioInput's doc comment. */
  rightExtra?: React.ReactNode;
  /** Mirrors recorder.live (phase 'recording' or 'paused') outward. Unmounting
   * this component while it's true silently loses whatever's been captured
   * -- teardown on unmount releases the mic without finalizing a file (see
   * useRecorder) -- so AudioInput uses this to warn before switching away to
   * the Upload tab rather than after the fact. */
  onLiveChange?: (live: boolean) => void;
}) {
  const platform = useMemo(() => detectPlatform(), []);
  const support = useMemo(() => sourceSupport(platform), [platform]);
  const blocked = useMemo(() => blockedReason(platform), [platform]);

  const [source, setSource] = useState<Source>('mic');
  const [deviceId, setDeviceId] = useState('');
  const [withMic, setWithMic] = useState(true);
  // Only meaningful once withMic actually produces a channel-separated
  // recording (source !== 'mic' && withMic) -- see the select below. Default
  // to the safe assumption: a mislabeled multi-person room is silent data
  // loss, a redundant diarization call on a genuinely single remote voice is
  // just a few wasted seconds.
  const [roomSpeakers, setRoomSpeakers] = useState<RoomSpeakers>('multiple');
  const [liveCaptionsOn, setLiveCaptionsOn] = useState(false);

  const recorder = useRecorder();
  const { devices, refresh, requestAccess, requesting, requestError } = useAudioInputs(true);

  // Owned here rather than inside LiveTranscriptPanel/InsightsPanel so both
  // can read the same captions instead of each opening their own websocket
  // (and paying for the periodic real transcription call behind it) for
  // what would be identical audio. Only meaningful in 'wide' layout --
  // 'compact' still renders LiveCaptionStrip below, which owns its own.
  const captionsLive = layout === 'wide' && liveCaptionsOn && recorder.phase === 'recording';
  const { captions, connected: captionsConnected } = useLiveCaption(
    recorder.liveStreams,
    captionsLive,
  );

  // Labels are blank until permission has been granted once, so re-read the
  // list the moment a recording succeeds -- that is when they appear.
  useEffect(() => {
    if (recorder.phase === 'recording') void refresh();
  }, [recorder.phase, refresh]);

  useEffect(() => {
    onLiveChange?.(recorder.live);
    // onLiveChange is expected to be a stable setState-style callback, same
    // reasoning as onModeChange in AudioInput -- not a dep so the host
    // doesn't have to memoize it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recorder.live]);

  useEffect(() => {
    onRecorded(
      recorder.clip?.file ?? null,
      recorder.clip?.durationSec ?? 0,
      recorder.clip?.channelMap ?? null,
      roomSpeakers,
    );
    // Re-fires on a roomSpeakers change too, not just a new clip -- the
    // selector stays editable after Stop (see its disabled= below), and
    // without this the page would keep whatever value was current the
    // instant recording finished.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recorder.clip, roomSpeakers]);

  const chosen = support[source];
  const canStart = chosen.available && !disabled;
  const needsDevicePermission = devices.length > 0 && !devices[0].label;
  // Only a plain mic recording releases the device on pause (see
  // useRecorder) -- pausing a tab/system capture just suspends the encoder,
  // so there is nothing to switch there.
  const canSwitchWhilePaused = recorder.phase === 'paused' && source === 'mic';

  // No capture API here at all. Say why, and how to get one -- "unsupported"
  // sends people to a different browser when the actual problem is the URL.
  if (blocked) {
    const origin = typeof window === 'undefined' ? '' : window.location.origin;
    const insecure = platform.hasRecorder && !platform.hasMediaDevices;
    return (
      <div className="space-y-3 rounded-xl border border-warning/40 bg-warning-soft/30 p-5">
        <p className="text-sm font-medium text-warning-ink">Recording is not available here</p>
        <p className="text-sm text-fg-muted">{blocked}</p>
        {insecure && (
          <>
            <p className="text-sm text-fg-muted">
              This page is <code className="font-mono text-xs">{origin}</code>. Any of these fixes it:
            </p>
            <ul className="list-disc space-y-1 pl-5 text-sm text-fg-muted">
              <li>
                Open it as <code className="font-mono text-xs">http://localhost:4020</code> from the
                machine it runs on — localhost counts as secure.
              </li>
              <li>Put it behind an HTTPS reverse proxy, and set MMN_SESSION_COOKIE_SECURE=true.</li>
              <li>
                Just to try it: allow this one origin under{' '}
                <code className="font-mono text-xs">
                  chrome://flags/#unsafely-treat-insecure-origin-as-secure
                </code>
                .
              </li>
            </ul>
          </>
        )}
        <p className="text-sm text-fg-muted">
          Uploading a file works regardless — record on your phone or laptop and drop it in.
        </p>
      </div>
    );
  }

  const controls = (
    <div className="space-y-4 rounded-xl border border-border bg-surface p-5">
      <div role="radiogroup" aria-label="Recording source" className="grid gap-2 sm:grid-cols-3">
        {SOURCES.map(({ id, label, icon: Icon }) => {
          const on = source === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={on}
              disabled={recorder.live || disabled}
              onClick={() => setSource(id)}
              className={cn(
                'flex items-center gap-2 rounded-lg border p-3 text-left text-sm transition-colors duration-fast',
                on
                  ? 'border-primary bg-primary-soft text-primary-soft-fg'
                  : 'border-border bg-surface-2/50 text-fg-muted hover:bg-surface-2',
                // Unsupported stays pickable: the hint underneath is the point,
                // and hiding it just makes people wonder why it is not offered.
                !support[id].available && 'opacity-70',
                (recorder.live || disabled) && 'cursor-not-allowed opacity-60',
              )}
            >
              <Icon className="size-4 shrink-0" aria-hidden />
              <span className="font-medium">{label}</span>
            </button>
          );
        })}
      </div>

      <p
        className={cn(
          'text-xs',
          chosen.available ? 'text-fg-subtle' : 'text-warning-ink',
        )}
      >
        {chosen.hint}
      </p>

      {/* Before the device select, which is the thing it governs. */}
      {source !== 'mic' && chosen.available && (
        <label className="flex items-center gap-2 text-sm text-fg-muted">
          <input
            type="checkbox"
            checked={withMic}
            disabled={recorder.live || disabled}
            onChange={(e) => setWithMic(e.target.checked)}
            className="size-4 rounded border-border-strong"
          />
          Also record my microphone
          <span className="text-xs text-fg-subtle">
            — without it you capture everyone but yourself
          </span>
        </label>
      )}

      {/* Only meaningful once a mic and a room stream both exist to keep
          apart -- see the ChannelMergerNode wiring in useRecorder. Editable
          after Stop too: the roomSpeakers effect above re-fires on change. */}
      {source !== 'mic' && withMic && chosen.available && (
        <div>
          <Label htmlFor="rec-room-speakers">On the other side</Label>
          <Select
            id="rec-room-speakers"
            className="mt-1.5"
            value={roomSpeakers}
            disabled={disabled}
            onChange={(e) => setRoomSpeakers(e.target.value as RoomSpeakers)}
          >
            <option value="multiple">Several people</option>
            <option value="single">Just one other person</option>
          </Select>
          <p className="mt-1 text-xs text-fg-subtle">
            We can tell your voice from everyone else's for free, from which side of the
            recording it came from. Saying there's only one other person skips guessing who's
            who on that side too.
          </p>
        </div>
      )}

      {(source === 'mic' || withMic) && (
        <div>
          <div className="flex items-center justify-between">
            <Label htmlFor="rec-device">
              {source === 'mic' ? 'Input' : 'Microphone to mix in'}
            </Label>
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              aria-label="Refresh device list"
              title={
                needsDevicePermission
                  ? 'Prompts for microphone access once, just to read device names'
                  : 'Refresh device list'
              }
              loading={requesting}
              disabled={(recorder.live && !canSwitchWhilePaused) || disabled}
              onClick={() => void (needsDevicePermission ? requestAccess() : refresh())}
            >
              <RefreshCw className="size-3.5" />
            </Button>
          </div>
          <Select
            id="rec-device"
            className="mt-1.5"
            value={deviceId}
            disabled={(recorder.live && !canSwitchWhilePaused) || disabled}
            onChange={(e) => setDeviceId(e.target.value)}
          >
            <option value="">System default</option>
            {/* Unlabelled devices are indistinguishable from each other, so a
                fabricated "Input 1" would be naming something we can't actually
                tell apart -- better to just not offer a choice until refreshing
                reveals which is which. */}
            {devices
              .filter((device) => device.label)
              .map((device) => (
                <option key={device.deviceId} value={device.deviceId}>
                  {device.label}
                </option>
              ))}
          </Select>
          {/* Pausing a mic recording releases the device entirely (see
              useRecorder), which is what makes it safe to offer a different
              one here -- Resume opens whichever is selected at that point. */}
          {canSwitchWhilePaused && (
            <p className="mt-1 text-xs text-fg-subtle">
              Pick a different input, then Resume.
            </p>
          )}
          {requestError && (
            <p role="alert" className="mt-1 text-xs text-danger-ink">
              {requestError}
            </p>
          )}
        </div>
      )}

      <label className="flex items-center gap-2 text-sm text-fg-muted">
        <input
          type="checkbox"
          checked={liveCaptionsOn}
          disabled={disabled}
          onChange={(e) => setLiveCaptionsOn(e.target.checked)}
          className="size-4 rounded border-border-strong"
        />
        Show live captions while recording
        <span className="text-xs text-fg-subtle">
          — a rough draft only; the real transcript is still built after you stop
        </span>
      </label>

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
        {!recorder.live ? (
          <Button
            type="button"
            variant="primary"
            loading={recorder.busy}
            disabled={!canStart}
            onClick={() => {
              // Starting over replaces recorder.clip in place -- there is no
              // separate "keep the old one" step, so this is the only chance
              // to back out before it's gone.
              if (
                recorder.clip &&
                !window.confirm(
                  'Recording again discards the current recording, unless you have already submitted it. Continue?',
                )
              ) {
                return;
              }
              void recorder.start({ source, deviceId: deviceId || undefined, withMic });
            }}
          >
            <Circle className="fill-current" />
            {recorder.clip ? 'Record again' : 'Start recording'}
          </Button>
        ) : (
          <>
            <Button type="button" variant="primary" onClick={recorder.stop}>
              <Square className="fill-current" />
              Stop
            </Button>
            {recorder.phase === 'recording' ? (
              <Button type="button" variant="secondary" onClick={recorder.pause}>
                <Pause />
                Pause
              </Button>
            ) : (
              <Button
                type="button"
                variant="secondary"
                loading={recorder.resuming}
                onClick={() => recorder.resume({ deviceId: deviceId || undefined })}
              >
                <Play />
                Resume
              </Button>
            )}
          </>
        )}

        {recorder.live && (
          <>
            <span
              className="inline-flex items-center gap-2 font-mono text-sm tabular"
              aria-live="off"
            >
              <span
                className={cn(
                  'size-2 rounded-full',
                  recorder.phase === 'recording' ? 'animate-pulse bg-danger' : 'bg-fg-faint',
                )}
                aria-hidden
              />
              {recorder.elapsed}
            </span>
            <LevelMeter level={recorder.level} active={recorder.phase === 'recording'} />
          </>
        )}
      </div>

      {/* Status in text, because the dot and the meter are both colour alone. */}
      <p className="sr-only" role="status">
        {recorder.phase === 'recording'
          ? `Recording, ${recorder.elapsed}`
          : recorder.phase === 'paused'
            ? `Paused at ${recorder.elapsed}`
            : recorder.clip
              ? 'Recording finished'
              : 'Not recording'}
      </p>

      {recorder.error && (
        <p role="alert" className="text-sm text-danger-ink">
          {recorder.error}
        </p>
      )}

      {/* In 'wide' layout this strip is replaced by the always-mounted
          LiveTranscriptPanel on the left -- rendering both would show the
          same rolling captions twice. */}
      {layout === 'compact' && liveCaptionsOn && recorder.phase === 'recording' && (
        <LiveCaptionStrip streams={recorder.liveStreams} enabled />
      )}

      {recorder.clip && !recorder.live && (
        <div className="space-y-2 rounded-lg border border-border bg-surface-2/50 p-3">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium">
              Recorded {fmtElapsedMs(recorder.clip.durationSec * 1000)}
            </p>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={disabled}
              onClick={recorder.discard}
            >
              <Trash2 />
              Discard
            </Button>
          </div>
          {/* Listen before committing minutes of GPU time to it. */}
          <audio controls src={recorder.clip.url} className="w-full" />
        </div>
      )}
    </div>
  );

  if (layout === 'compact') return controls;

  return (
    <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_460px]">
      {/* Transcript and Insights share the left column and the one caption
          feed above -- Insights is the analysis of what's shown here, so it
          reads as "under the transcript", not a separate unrelated panel. */}
      <div className="space-y-4">
        <LiveTranscriptPanel
          captions={captions}
          connected={captionsConnected}
          enabled={captionsLive}
        />
        <InsightsPanel captions={captions} enabled={captionsLive} />
      </div>
      {/* Controls and the rest of the meeting form (title, when, thread,
          submit -- see NewMeetingPage's rightExtra) share this column, so
          "set up the meeting" reads as one right-hand task next to the
          transcript rather than two stacked forms. */}
      <div className="space-y-4">
        {controls}
        {rightExtra}
      </div>
    </div>
  );
}
