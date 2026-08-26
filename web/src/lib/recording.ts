/**
 * What the browser will and will not record, and what to call the result.
 *
 * The capability matrix here is the whole reason this file exists. Audio capture
 * is the least uniform corner of the web platform, and the failure mode is
 * silent in the literal sense: ask Chrome on macOS for system audio and you get
 * a perfectly valid recording of nothing at all. Better to say up front which
 * sources this browser can actually deliver.
 */

import asrLanguageSupportData from './asrLanguageSupport.json';

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
  /**
   * Whether `navigator.mediaDevices` exists at all.
   *
   * It is gated on a secure context, so on a plain-HTTP LAN address -- which is
   * exactly how this app is usually reached -- the entire object is *undefined*
   * rather than the calls failing. `MediaRecorder` is not gated, so feature-
   * detecting on that alone reports a recorder that cannot open an input.
   */
  hasMediaDevices: boolean;
}

export interface Env {
  hasDisplayMedia?: boolean;
  hasRecorder?: boolean;
  hasMediaDevices?: boolean;
}

export function detectPlatform(
  ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent,
  env: Env = {},
): Platform {
  const safari = /^((?!chrome|android|crios|fxios).)*safari/i.test(ua);
  const {
    hasMediaDevices = typeof navigator !== 'undefined' &&
      !!navigator.mediaDevices?.getUserMedia,
    hasDisplayMedia = typeof navigator !== 'undefined' &&
      !!navigator.mediaDevices?.getDisplayMedia,
    hasRecorder = typeof window !== 'undefined' && 'MediaRecorder' in window,
  } = env;

  return {
    mac: /Mac|iPhone|iPad/i.test(ua),
    firefox: /firefox|fxios/i.test(ua),
    safari,
    chromium: /chrome|chromium|crios|edg\//i.test(ua) && !safari,
    hasDisplayMedia,
    hasRecorder,
    hasMediaDevices,
  };
}

/** Why recording is off the table here, or null if it is available. */
export function blockedReason(platform: Platform): string | null {
  if (!platform.hasRecorder) {
    return 'This browser cannot record audio. Chrome, Edge, Firefox or Safari 14.1 and later can.';
  }
  if (!platform.hasMediaDevices) {
    return 'Recording needs a secure page. Browsers only hand out the microphone over HTTPS or on localhost, so on a plain http:// address they hide it entirely.';
  }
  return null;
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
  const { mac, firefox, safari, chromium, hasDisplayMedia, hasMediaDevices } = platform;

  // No capture API at all -- an insecure origin, most likely. Nothing is
  // offered, and the panel explains it once rather than three times.
  const blocked = blockedReason(platform);
  if (blocked) {
    return {
      mic: { available: false, hint: blocked },
      tab: { available: false, hint: blocked },
      system: { available: false, hint: blocked },
    };
  }

  const displayAudio = chromium && hasDisplayMedia && hasMediaDevices;
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

/**
 * Generic live-caption language choices, offered as a picklist rather than
 * free text. The Whisper-shaped ISO-639-1 convention ("en"), not a language
 * name ("english") -- confirmed against a real streaming ASR backend that
 * the latter doesn't get rejected, it silently breaks streaming entirely,
 * with an error that never mentions language. A picklist makes that typo
 * structurally impossible instead of documenting it in a hint underneath a
 * text box. `''` means "auto-detect per window".
 *
 * This is the *fallback* for a model asrLanguageSupport.json doesn't know
 * about, or one explicitly marked "unbounded" there (Whisper/VibeVoice-
 * shaped, ~50-100 languages, not worth hardcoding into a short list) -- see
 * captionLanguagesForModel below, which is what callers actually want.
 */
export const LIVE_CAPTION_LANGUAGES: { code: string; label: string }[] = [
  { code: '', label: 'Auto-detect' },
  { code: 'en', label: 'English' },
  { code: 'es', label: 'Spanish' },
  { code: 'fr', label: 'French' },
  { code: 'de', label: 'German' },
  { code: 'pt', label: 'Portuguese' },
  { code: 'it', label: 'Italian' },
  { code: 'nl', label: 'Dutch' },
  { code: 'ru', label: 'Russian' },
  { code: 'zh', label: 'Chinese' },
  { code: 'ja', label: 'Japanese' },
  { code: 'ko', label: 'Korean' },
  { code: 'hi', label: 'Hindi' },
  { code: 'ar', label: 'Arabic' },
];

interface ModelLanguageEntry {
  note?: string;
  sources?: string[];
  /** Whisper/VibeVoice-shaped: broad, near-universal coverage. No
   * confirmed/documented list is worth hardcoding -- LIVE_CAPTION_LANGUAGES
   * above is offered instead. */
  unbounded?: boolean;
  /** Codes actually observed to work (or fail) against a real running
   * instance of this app -- what the picker restricts itself to. */
  confirmed?: { code: string; label: string }[];
  /** The wider set the model's own vendor docs claim, kept as a worklist
   * even though the picker doesn't offer it directly -- see
   * asrLanguageSupport.json's own _readme for how to promote one by hand. */
  documented?: { code: string; label: string }[];
}

const ASR_LANGUAGE_SUPPORT = (asrLanguageSupportData as { models: Record<string, ModelLanguageEntry> })
  .models;

/**
 * confirmed ++ whatever's in documented but not already in confirmed, the
 * latter suffixed "(untested here)" -- picking one *replaces* the other
 * (`confirmed ?? documented`) would make documented dead weight the picker
 * never actually offers, which is exactly the bug this replaced: selecting
 * nemotron-3.5-asr-streaming-0.6b showed only English/Spanish even though
 * its own entry documents 40 locales. Showing the untested ones (clearly
 * labelled) rather than hiding them is what "I can change manually later"
 * is for -- picking one is also how an entry gets promoted from documented
 * to confirmed by hand once someone's actually tried it. A pick that fails
 * is no longer silent either, now that channel_worker_livestt/channel_worker
 * relay a worker-init failure as {"type": "warning", ...} (see
 * useLiveCaption's CaptionWarning).
 */
function mergedLanguages(entry: ModelLanguageEntry): { code: string; label: string }[] {
  const confirmed = entry.confirmed ?? [];
  const confirmedCodes = new Set(confirmed.map((l) => l.code));
  const untested = (entry.documented ?? [])
    .filter((l) => !confirmedCodes.has(l.code))
    .map((l) => ({ code: l.code, label: `${l.label} (untested here)` }));
  return [...confirmed, ...untested];
}

/**
 * Which language codes to offer for a given ASR model -- see
 * asrLanguageSupport.json (which this reads) for where the data comes from
 * and how to edit it by hand as more codes get confirmed against a real
 * deployment. A model that file doesn't mention, or one marked "unbounded"
 * (Whisper/VibeVoice-shaped), falls back to the generic list above rather
 * than guessing at a restriction that might be wrong.
 *
 * Keyed by *model name*, not backend: the three live-caption backends'
 * default models (nemotron-3.5-asr-streaming-0.6b, realtime_eou_120m-v1,
 * lfm2.5-audio-1.5b-realtime) turn out to have three different, all-narrow
 * language ceilings -- a backend-level list would have missed that the
 * /v1/realtime backend's own default model is English-only too, not just
 * live_stt's.
 */
export function captionLanguagesForModel(
  model: string | null | undefined,
): { code: string; label: string }[] {
  const entry = model ? ASR_LANGUAGE_SUPPORT[model] : undefined;
  if (!entry || entry.unbounded) return LIVE_CAPTION_LANGUAGES;
  const merged = mergedLanguages(entry);
  return merged.length > 0 ? merged : LIVE_CAPTION_LANGUAGES;
}

/**
 * Like captionLanguagesForModel above, but `undefined` -- not the generic
 * fallback -- for a model with no confirmed/documented restriction. Meant
 * for a UI that has its own free-text fallback (the Settings page's
 * language field): a fixed picklist can't usefully enumerate Whisper's
 * ~99 languages, so there's no dropdown worth forcing there, unlike
 * RecorderPanel's language <select>, which has no free-text mode and wants
 * captionLanguagesForModel's generic-list fallback instead.
 */
export function restrictedLanguagesForModel(
  model: string | null | undefined,
): { code: string; label: string }[] | undefined {
  const entry = model ? ASR_LANGUAGE_SUPPORT[model] : undefined;
  if (!entry || entry.unbounded) return undefined;
  const merged = mergedLanguages(entry);
  return merged.length > 0 ? merged : undefined;
}

/** A one-line reason the language picker is restricted for this model, for
 * a hint under the dropdown -- undefined when nothing is known about it (or
 * it's unbounded, where no explanation is needed since the full list is
 * offered). */
export function languageSupportNoteFor(model: string | null | undefined): string | undefined {
  const entry = model ? ASR_LANGUAGE_SUPPORT[model] : undefined;
  return entry && !entry.unbounded ? entry.note : undefined;
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
