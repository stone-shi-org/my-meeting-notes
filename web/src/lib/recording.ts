/**
 * What the browser will and will not record, and what to call the result.
 *
 * The capability matrix here is the whole reason this file exists. Audio capture
 * is the least uniform corner of the web platform, and the failure mode is
 * silent in the literal sense: ask Chrome on macOS for system audio and you get
 * a perfectly valid recording of nothing at all. Better to say up front which
 * sources this browser can actually deliver.
 */

export type Source = 'mic' | 'tab' | 'system';

export interface Capability {
  /** Whether to let the user start a recording from this source at all. */
  available: boolean;
  /** Shown under the option either way -- as guidance, or as the reason not. */
  hint: string;
}

export interface Platform {
  mac: boolean;
  firefox: boolean;
  safari: boolean;
  /** Chrome, Edge, Brave, Opera: the engines that implement display audio. */
  chromium: boolean;
  hasDisplayMedia: boolean;
  hasRecorder: boolean;
}

export function detectPlatform(
  ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent,
  hasDisplayMedia = typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getDisplayMedia,
  hasRecorder = typeof window !== 'undefined' && 'MediaRecorder' in window,
): Platform {
  const safari = /^((?!chrome|android|crios|fxios).)*safari/i.test(ua);
  return {
    mac: /Mac|iPhone|iPad/i.test(ua),
    firefox: /firefox|fxios/i.test(ua),
    safari,
    chromium: /chrome|chromium|crios|edg\//i.test(ua) && !safari,
    hasDisplayMedia,
    hasRecorder,
  };
}

/**
 * Which sources this browser can really deliver audio from.
 *
 * The two rules worth knowing, both learned the hard way rather than from a
 * spec table:
 *
 * * **Only Chromium delivers `getDisplayMedia` audio.** Firefox and Safari
 *   implement the call and hand back a video track with no audio, so the
 *   recording succeeds and is empty.
 * * **macOS has no system-audio loopback for the browser.** Tab audio works
 *   there; "Entire screen" does not, because the OS gives Chrome nothing to
 *   capture. Windows and ChromeOS do have it. The way round it on a Mac is a
 *   virtual output device, which then shows up as an *input* -- i.e. in the
 *   microphone list, not here.
 */
export function sourceSupport(platform: Platform): Record<Source, Capability> {
  const { mac, firefox, safari, chromium, hasDisplayMedia } = platform;

  const displayAudio = chromium && hasDisplayMedia;
  const noDisplayAudio = firefox
    ? 'Firefox does not share audio with a screen or tab, only video. Use Chrome or Edge, or record the microphone.'
    : safari
      ? 'Safari does not share audio with a screen or tab, only video. Use Chrome or Edge, or record the microphone.'
      : 'This browser does not offer screen or tab audio. Use Chrome or Edge, or record the microphone.';

  return {
    mic: {
      available: true,
      hint: 'Your microphone. Also where a loopback device such as BlackHole or Loopback shows up once installed.',
    },
    tab: {
      available: displayAudio,
      hint: displayAudio
        ? 'Pick the tab your call is in and leave “Also share tab audio” ticked — without it the recording is silent.'
        : noDisplayAudio,
    },
    system: {
      available: displayAudio && !mac,
      hint: !displayAudio
        ? noDisplayAudio
        : mac
          ? 'macOS does not let a browser record other apps, whatever it is sharing. To capture a desktop app, install a loopback device (BlackHole, Loopback), send the app’s output to it, and choose it under Microphone.'
          : 'Choose “Entire screen” and tick “Also share system audio”. Sharing a single window captures no audio.',
    },
  };
}

/** Container preference, best first. Opus in WebM everywhere but Safari. */
export const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  'audio/ogg;codecs=opus',
] as const;

/**
 * The best container this browser can write, or null if it can write none.
 *
 * Passing an empty string to MediaRecorder would let it choose, but then we
 * cannot name the file correctly, and the upload route validates on extension.
 */
export function pickMimeType(
  isSupported: (type: string) => boolean = (type) =>
    typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(type),
  candidates: readonly string[] = MIME_CANDIDATES,
): string | null {
  return candidates.find((type) => isSupported(type)) ?? null;
}

/** Extension for a recorded blob. Must be one the upload route accepts. */
export function extensionFor(mime: string): string {
  const base = mime.split(';')[0].trim().toLowerCase();
  switch (base) {
    case 'audio/mp4':
      // .m4a rather than .mp4: it is audio, and both are accepted anyway.
      return '.m4a';
    case 'audio/ogg':
      return '.ogg';
    case 'audio/webm':
    default:
      return '.webm';
  }
}

const SOURCE_SLUG: Record<Source, string> = {
  mic: 'microphone',
  tab: 'tab-audio',
  system: 'system-audio',
};

/** `recording-tab-audio-2026-07-28-1432.webm` -- sortable, and says what it is. */
export function recordingFilename(source: Source, mime: string, at: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())}` +
    `-${p(at.getHours())}${p(at.getMinutes())}`;
  return `recording-${SOURCE_SLUG[source]}-${stamp}${extensionFor(mime)}`;
}

/**
 * Audio constraints per source.
 *
 * Mic gets the usual conference processing. Captured audio must not: echo
 * cancellation and noise suppression are tuned for a person in a room, and
 * applied to a clean tab feed they gate and pump the far end's speech, which is
 * the very thing being recorded.
 */
export function audioConstraints(source: Source, deviceId?: string): MediaTrackConstraints {
  if (source === 'mic') {
    return {
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    };
  }
  return { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
}

/** `12:04`, counting up. Recording length, not a media position. */
export function fmtElapsedMs(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const p = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}
