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
import { useAudioInputs, useRecorder } from '@/hooks/useRecorder';
import { cn } from '@/lib/cn';
import {
  blockedReason,
  detectPlatform,
  fmtElapsedMs,
  sourceSupport,
  type Source,
} from '@/lib/recording';

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
}: {
  /** Handed the finished clip as a File, ready for the normal upload path. */
  onRecorded: (file: File | null, durationSec: number) => void;
  disabled?: boolean;
}) {
  const platform = useMemo(() => detectPlatform(), []);
  const support = useMemo(() => sourceSupport(platform), [platform]);
  const blocked = useMemo(() => blockedReason(platform), [platform]);

  const [source, setSource] = useState<Source>('mic');
  const [deviceId, setDeviceId] = useState('');
  const [withMic, setWithMic] = useState(true);

  const recorder = useRecorder();
  const { devices, refresh, requestAccess, requesting, requestError } = useAudioInputs(true);

  // Labels are blank until permission has been granted once, so re-read the
  // list the moment a recording succeeds -- that is when they appear.
  useEffect(() => {
    if (recorder.phase === 'recording') void refresh();
  }, [recorder.phase, refresh]);

  useEffect(() => {
    onRecorded(recorder.clip?.file ?? null, recorder.clip?.durationSec ?? 0);
    // onRecorded is a setter from the page; re-running on identity changes
    // would fight the page's own state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recorder.clip]);

  const chosen = support[source];
  const canStart = chosen.available && !disabled;
  const needsDevicePermission = devices.length > 0 && !devices[0].label;

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

  return (
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
              disabled={recorder.live || disabled}
              onClick={() => void (needsDevicePermission ? requestAccess() : refresh())}
            >
              <RefreshCw className="size-3.5" />
            </Button>
          </div>
          <Select
            id="rec-device"
            className="mt-1.5"
            value={deviceId}
            disabled={recorder.live || disabled}
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
          {requestError && (
            <p role="alert" className="mt-1 text-xs text-danger-ink">
              {requestError}
            </p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
        {!recorder.live ? (
          <Button
            type="button"
            variant="primary"
            loading={recorder.busy}
            disabled={!canStart}
            onClick={() => void recorder.start({ source, deviceId: deviceId || undefined, withMic })}
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
              <Button type="button" variant="secondary" onClick={recorder.resume}>
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
}
