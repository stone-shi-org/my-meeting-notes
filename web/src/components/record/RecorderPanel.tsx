import { useQuery } from '@tanstack/react-query';
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
import { type ActivityState, useLiveCaption } from '@/hooks/useLiveCaption';
import { useAudioInputs, useRecorder } from '@/hooks/useRecorder';
import { api } from '@/lib/api';
import { cn } from '@/lib/cn';
import {
  blockedReason,
  detectPlatform,
  fmtElapsedMs,
  LIVE_CAPTION_LANGUAGES,
  sourceSupport,
  type Source,
} from '@/lib/recording';
import type { SettingEntry } from '@/types/api';

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
function LevelMeter({
  level,
  active,
  label = 'Input level',
  bars = 20,
}: {
  level: number;
  active: boolean;
  /** Overrides the default aria-label -- used when two meters are on screen
   * at once and "Input level" would no longer say which one this is. */
  label?: string;
  /** Fewer bars when two meters share the row that used to hold one. */
  bars?: number;
}) {
  const lit = Math.round(level * bars);
  return (
    <div
      className="flex items-center gap-[3px]"
      role="meter"
      aria-valuenow={Math.round(level * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
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

const ACTIVITY_COLOR: Record<ActivityState, string> = {
  idle: 'bg-fg-faint',
  buffering: 'bg-warning',
  calling: 'bg-primary animate-pulse',
};

const ACTIVITY_LABEL: Record<ActivityState, string> = {
  idle: 'idle',
  buffering: 'speech detected',
  calling: 'transcribing',
};

/**
 * Live-transcript activity for one channel -- see live_caption.py's
 * channel_worker/_handle_realtime_event for what actually drives
 * idle/buffering/calling (server VAD heard speech start/stop, or nothing is
 * happening). Colour-only like the recording dot above it, so it carries an
 * sr-only equivalent via `title` isn't enough on its own -- LevelMeters
 * below also renders the text form next to it.
 */
function ActivityDot({ state }: { state: ActivityState }) {
  return (
    <span
      className={cn('size-1.5 shrink-0 rounded-full', ACTIVITY_COLOR[state])}
      aria-hidden
      title={`Live transcript: ${ACTIVITY_LABEL[state]}`}
    />
  );
}

/**
 * One meter, or two side by side when the recording is keeping mic and room
 * on separate channels (`recorder.mixing` -- see useRecorder). A single
 * merged meter there would report whichever side is momentarily louder and
 * hide a genuinely silent one behind it -- the "missed the share-tab-audio
 * checkbox" failure this panel exists to catch, just on the room side
 * instead of the mic side.
 */
function LevelMeters({
  level,
  levelRoom,
  mixing,
  active,
  activity,
}: {
  level: number;
  levelRoom: number;
  mixing: boolean;
  active: boolean;
  /** Live-caption pipeline state per channel, from useLiveCaption --
   * omitted entirely when live captions are off, since there is no
   * channel_worker for the dot to describe then. */
  activity?: { me: ActivityState; room: ActivityState };
}) {
  if (!mixing) {
    return (
      <div className="flex items-center gap-1.5">
        {activity && <ActivityDot state={activity.me} />}
        <LevelMeter level={level} active={active} />
      </div>
    );
  }
  return (
    <div className="flex items-center gap-3">
      <div className="flex items-center gap-1.5">
        {activity && <ActivityDot state={activity.me} />}
        <span className="text-xs text-fg-subtle">You</span>
        <LevelMeter level={level} active={active} label="Your microphone level" bars={12} />
      </div>
      <div className="flex items-center gap-1.5">
        {activity && <ActivityDot state={activity.room} />}
        <span className="text-xs text-fg-subtle">Room</span>
        <LevelMeter level={levelRoom} active={active} label="Room audio level" bars={12} />
      </div>
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
  /** Handed the finished clip as a File, ready for the normal upload path. */
  onRecorded: (file: File | null, durationSec: number) => void;
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
  const [liveCaptionsOn, setLiveCaptionsOn] = useState(false);

  // Settings -> Live captions' value is only the *default* -- see
  // live_caption_language's doc comment in config.py. `null` here means
  // "hasn't been touched yet", so this panel keeps tracking that default
  // (which may still be loading) until the user actually picks something,
  // rather than locking in a blank value before the request resolves.
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<{ settings: Record<string, SettingEntry> }>('/settings'),
  });
  const defaultCaptionLanguage = String(
    settingsQuery.data?.settings.live_caption_language?.value ?? '',
  );
  const [captionLanguageChoice, setCaptionLanguageChoice] = useState<string | null>(null);
  const captionLanguage = captionLanguageChoice ?? defaultCaptionLanguage;

  const recorder = useRecorder();
  const { devices, refresh, requestAccess, requesting, requestError } = useAudioInputs(true);

  // Owned here rather than inside LiveTranscriptPanel/InsightsPanel so both
  // can read the same captions instead of each opening their own websocket
  // (and paying for the periodic real transcription call behind it) for
  // what would be identical audio. Only meaningful in 'wide' layout --
  // 'compact' still renders LiveCaptionStrip below, which owns its own.
  const captionsLive = layout === 'wide' && liveCaptionsOn && recorder.phase === 'recording';
  const {
    captions,
    connected: captionsConnected,
    activity: captionsActivity,
    backend: captionsBackend,
    partial: captionsPartial,
  } = useLiveCaption(recorder.liveStreams, captionsLive, captionLanguage);

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
    onRecorded(recorder.clip?.file ?? null, recorder.clip?.durationSec ?? 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recorder.clip]);

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

  // Once a 'wide' recording is actually live, the setup parameters (source,
  // device, live-caption toggle/language, all their hints) have nothing left
  // to decide -- they're locked in for this recording anyway (see their own
  // disabled= checks below) -- and take up the column the transcript panel
  // now needs (see the wide-layout return below). This condensed block is
  // what that column shows instead: just enough to stop/pause/resume and
  // confirm the input is alive. 'compact' layout (the narrower "add a
  // recording" page) never swaps to this -- it has no spare column to hand
  // the transcript, so its controls stay fully expanded throughout.
  const liveCompactControls = (
    <div className="space-y-4 rounded-xl border border-border bg-surface p-5">
      <div className="flex flex-wrap items-center gap-3">
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
        <span className="inline-flex items-center gap-2 font-mono text-sm tabular" aria-live="off">
          <span
            className={cn(
              'size-2 rounded-full',
              recorder.phase === 'recording' ? 'animate-pulse bg-danger' : 'bg-fg-faint',
            )}
            aria-hidden
          />
          {recorder.elapsed}
        </span>
      </div>

      <LevelMeters
        level={recorder.level}
        levelRoom={recorder.levelRoom}
        mixing={recorder.mixing}
        active={recorder.phase === 'recording'}
        activity={captionsLive ? captionsActivity : undefined}
      />

      {/* Status in text, because the dot and the meter are both colour alone. */}
      <p className="sr-only" role="status">
        {recorder.phase === 'recording' ? `Recording, ${recorder.elapsed}` : `Paused at ${recorder.elapsed}`}
        {captionsLive &&
          ` — live transcript: ${ACTIVITY_LABEL[captionsActivity.me]} (you), ${ACTIVITY_LABEL[captionsActivity.room]} (room)`}
      </p>

      {recorder.error && (
        <p role="alert" className="text-sm text-danger-ink">
          {recorder.error}
        </p>
      )}
    </div>
  );

  const fullControls = (
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

      {/* Locked once live: the language is sent when the caption websocket
          first connects (see useLiveCaption), and a rolling window is short
          enough that auto-detect can misfire mid-recording -- picking this
          before Start is the point, not something to reconsider mid-call. */}
      {liveCaptionsOn && (
        <div>
          <Label htmlFor="live-caption-language">Live caption language</Label>
          <Select
            id="live-caption-language"
            className="mt-1.5"
            value={captionLanguage}
            disabled={recorder.live || disabled}
            onChange={(e) => setCaptionLanguageChoice(e.target.value)}
          >
            {LIVE_CAPTION_LANGUAGES.map(({ code, label }) => (
              <option key={code} value={code}>
                {label}
              </option>
            ))}
          </Select>
          <p className="mt-1 text-xs text-fg-subtle">
            Defaults to whatever Settings → Live captions has set. Pick a specific language if
            captions sometimes come back in the wrong one.
          </p>
        </div>
      )}

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
            <LevelMeters
              level={recorder.level}
              levelRoom={recorder.levelRoom}
              mixing={recorder.mixing}
              active={recorder.phase === 'recording'}
            />
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
          LiveTranscriptPanel further down the same column -- rendering both
          would show the same rolling captions twice. */}
      {layout === 'compact' && liveCaptionsOn && recorder.phase === 'recording' && (
        <LiveCaptionStrip streams={recorder.liveStreams} enabled language={captionLanguage} />
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

  if (layout === 'compact') return fullControls;

  // Live shrinks the right column's own controls (see liveCompactControls'
  // doc comment) so the transcript panel below it -- moved here from the
  // left column precisely to use that freed-up room -- has enough width to
  // be worth reading during the call, not just after.
  const controls = recorder.live ? liveCompactControls : fullControls;

  return (
    <div className="grid items-start gap-4 lg:grid-cols-2">
      {/* Insights is the analysis of the transcript in the right column --
          it stays on its own here so a long meeting's topic/question list
          doesn't compete with the transcript for the same column. */}
      <div className="space-y-4">
        <InsightsPanel captions={captions} enabled={captionsLive} />
      </div>
      {/* Controls, the transcript, and the rest of the meeting form (title,
          when, thread, submit -- see NewMeetingPage's rightExtra) share this
          column, so "set up the meeting" and "watch it happen" both read as
          right-hand tasks next to Insights rather than two stacked forms. */}
      <div className="space-y-4">
        {controls}
        <LiveTranscriptPanel
          captions={captions}
          connected={captionsConnected}
          enabled={captionsLive}
          backend={captionsBackend}
          partial={captionsPartial}
        />
        {rightExtra}
      </div>
    </div>
  );
}
