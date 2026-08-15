import { useCallback, useEffect, useRef, useState } from 'react';
import {
  audioConstraints,
  fmtElapsedMs,
  pickMimeType,
  recordingFilename,
  type Source,
} from '@/lib/recording';

export type Phase = 'idle' | 'starting' | 'recording' | 'paused' | 'done';

/**
 * Set when mic + tab/system were both captured and kept on separate stereo
 * channels rather than mixed to mono -- see the ChannelMergerNode wiring in
 * `start()`. 'mic_room' is the only shape today: channel 0 is whatever the
 * tab/system capture picked up ("the room"), channel 1 is the microphone.
 * Sent to the upload route so the backend can skip the model diarizer and use
 * this ground truth instead (see app/services/diarize.py's
 * diarize_channels_file).
 */
export type ChannelMap = 'mic_room' | null;

export interface Clip {
  file: File;
  /** Wall-clock length of the recording, which the WebM header will not carry. */
  durationSec: number;
  /** Object URL for the review player. Revoked when the clip is replaced. */
  url: string;
  channelMap: ChannelMap;
}

/**
 * Raw per-channel streams for the live-caption tap (useLiveCaption), kept
 * separate from the AudioContext graph above that feeds MediaRecorder.
 * Exposing the streams themselves rather than an AudioContext keeps the two
 * features independent -- a bug in one's audio graph cannot touch the
 * other's, and MediaRecorder's own tap is unaffected by whatever
 * useLiveCaption does with these.
 */
export interface LiveStreams {
  room: MediaStream | null;
  me: MediaStream | null;
}

export interface StartOptions {
  source: Source;
  deviceId?: string;
  /** Mix the microphone into a tab or system capture. */
  withMic?: boolean;
}

/** Chunk interval. Small enough that a crash costs a second, not the meeting. */
const TIMESLICE_MS = 1000;

/**
 * One recording, from permission prompt to a File ready for the upload route.
 *
 * Three decisions worth stating:
 *
 * **WebAudio meters, but only mixes when there is something to mix.** An
 * AudioContext renders against the audio hardware's clock, and on a machine
 * with no usable output device that clock runs *behind* wall clock -- measured
 * at 2.5s per 3s on a box with no sound card. Anything recorded through the
 * graph is silently time-compressed by the same factor. So the analyser hangs
 * off the sources as a tap, and the recorder is handed the raw track unless the
 * user asked to mix their microphone into a capture, which genuinely needs a
 * mixer.
 *
 * **The level meter is not decoration.** The commonest failure here is a
 * capture that is silent because the user missed Chrome's "share tab audio"
 * checkbox, and without a meter they find that out after the meeting.
 *
 * **A display capture keeps its video track alive.** We record only the audio,
 * but stopping the video track ends the share, and Chrome then ends the audio
 * with it. The frames are pulled and dropped; the browser's own "sharing"
 * indicator staying up is honest, since it is.
 *
 * **The screen wake lock is best-effort, not load-bearing.** It stops the
 * *screen* from dimming or locking mid-recording -- the actual failure mode
 * this exists for is a laptop lid staying open but the display sleeping, which
 * on some OS/browser combinations is enough to throttle a background tab hard
 * enough to starve MediaRecorder. It cannot promise anything once the tab is
 * genuinely backgrounded or the OS suspends the device outright, it is
 * unsupported outside Chromium and Safari 16.4+, and a lock is silently
 * dropped by the browser the moment the document goes hidden -- so it is
 * re-requested on `visibilitychange` rather than assumed to still hold.
 *
 * **Pausing a plain mic recording ends its MediaRecorder rather than
 * suspending it.** That is what releases the device: `.pause()` alone would
 * leave the old track open, and there would be nothing to switch to on
 * resume. Each pause/resume cycle is its own segment (own getUserMedia, own
 * AudioContext tap, own MediaRecorder), and the file at Stop is every
 * segment's blob concatenated. Verified against this app's own
 * `convert_to_wav16k_mono` -- ffmpeg decodes straight through the boundary
 * between two independently-finalized "live" WebM/Opus blobs (the exact
 * shape `MediaRecorder` produces) with only a ~6ms artifact per splice from
 * Opus's per-stream pre-skip, not the truncation a naive read might expect.
 * Tab/system capture is exempt: there is no device to switch mid-share, and
 * re-requesting `getDisplayMedia` on resume would force a fresh, disruptive
 * share prompt for no reason, so it keeps the old suspend/resume behaviour.
 */
export function useRecorder() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [level, setLevel] = useState(0);
  const [clip, setClip] = useState<Clip | null>(null);
  const [liveStreams, setLiveStreams] = useState<LiveStreams>({ room: null, me: null });

  const [resuming, setResuming] = useState(false);

  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<BlobPart[]>([]);
  const streams = useRef<MediaStream[]>([]);
  // Set once per recording, at start() -- whether mic + room ended up on
  // separate stereo channels. Read when the Clip is built, at Stop.
  const channelMapRef = useRef<ChannelMap>(null);
  const audioCtx = useRef<AudioContext | null>(null);
  const analyser = useRef<AnalyserNode | null>(null);
  const raf = useRef<number | null>(null);
  const ticker = useRef<ReturnType<typeof setInterval> | null>(null);
  // Elapsed is derived from timestamps rather than counted, so a throttled
  // background tab cannot make the displayed length drift from the real one.
  const startedAt = useRef(0);
  const accumulated = useRef(0);
  const startedWith = useRef<Source>('mic');
  // Mirrors clip.url so unmount can revoke it without the effect closing over
  // a stale clip.
  const clipUrl = useRef<string | null>(null);
  // A plain mic recording is split into one MediaRecorder per pause/resume
  // cycle rather than one continuous one, which is what makes it possible to
  // resume on a different device -- see openMicSegment below. Each finished
  // segment lands here; the file at Stop is all of them back to back.
  const segments = useRef<Blob[]>([]);
  const mimeTypeRef = useRef('');
  // Set by stop() right before ending the current segment, so that segment's
  // onstop knows whether it just ended a pause (keep going) or the recording
  // itself (build the file). pause() never touches this.
  const isFinalStop = useRef(false);
  const wakeLock = useRef<WakeLockSentinel | null>(null);
  // Whether a recording session wants the lock held, independent of whether
  // one is currently acquired -- the lock itself is dropped every time the
  // tab is hidden, but the session isn't over just because the tab was.
  const wantsWakeLock = useRef(false);

  const acquireWakeLock = useCallback(async () => {
    if (!('wakeLock' in navigator) || wakeLock.current) return;
    try {
      wakeLock.current = await navigator.wakeLock.request('screen');
    } catch {
      // Battery saver, an already-hidden tab, or no support -- recording
      // works either way, it just cannot promise the screen stays on.
      wakeLock.current = null;
    }
  }, []);

  const releaseWakeLock = useCallback(() => {
    wantsWakeLock.current = false;
    const sentinel = wakeLock.current;
    wakeLock.current = null;
    void sentinel?.release().catch(() => {});
  }, []);

  // Re-request on return to the tab: the browser silently releases the lock
  // the moment the document goes hidden, and does not restore it on its own.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === 'visible' && wantsWakeLock.current) {
        void acquireWakeLock();
      }
    }
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [acquireWakeLock]);

  const teardown = useCallback(() => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
    raf.current = null;
    if (ticker.current !== null) clearInterval(ticker.current);
    ticker.current = null;
    releaseWakeLock();

    for (const stream of streams.current) {
      for (const track of stream.getTracks()) track.stop();
    }
    streams.current = [];

    analyser.current?.disconnect();
    analyser.current = null;
    void audioCtx.current?.close().catch(() => {});
    audioCtx.current = null;
    recorder.current = null;
    setLevel(0);
    // The tracks just stopped above are the same streams useLiveCaption taps;
    // clearing this is what tells it to close its socket and tear down its
    // own AudioContexts rather than tapping dead tracks.
    setLiveStreams({ room: null, me: null });
  }, [releaseWakeLock]);

  // A recording in flight when the page unmounts must release the microphone
  // and drop the browser's sharing indicator; a finished one must not leave its
  // blob pinned in memory.
  useEffect(
    () => () => {
      teardown();
      if (clipUrl.current) URL.revokeObjectURL(clipUrl.current);
      clipUrl.current = null;
    },
    [teardown],
  );

  const meter = useCallback(() => {
    const node = analyser.current;
    if (!node) return;
    const samples = new Uint8Array(node.fftSize);
    node.getByteTimeDomainData(samples);

    // Peak deviation from silence (128), which tracks speech far better than
    // RMS at this update rate -- RMS reads near zero between syllables.
    let peak = 0;
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample - 128));
    const now = Math.min(1, peak / 128);
    // Fall away over about half a second rather than snapping. Speech is mostly
    // gaps at frame rate, and a meter that flickers to zero between words reads
    // as "not working" -- the exact thing it exists to rule out.
    setLevel((previous) => (now > previous ? now : previous * 0.85));

    raf.current = requestAnimationFrame(meter);
  }, []);

  /** Build the final File from every segment recorded so far, and tear down. */
  const finalizeRecording = useCallback(() => {
    const blob = new Blob(segments.current, { type: mimeTypeRef.current });
    const at = new Date();
    const durationSec =
      (accumulated.current + (startedAt.current ? Date.now() - startedAt.current : 0)) / 1000;
    const file = new File(
      [blob],
      recordingFilename(startedWith.current, mimeTypeRef.current, at),
      { type: mimeTypeRef.current, lastModified: at.getTime() },
    );
    const url = URL.createObjectURL(blob);
    clipUrl.current = url;
    setClip((previous) => {
      if (previous) URL.revokeObjectURL(previous.url);
      return { file, durationSec, url, channelMap: channelMapRef.current };
    });
    setPhase('done');
    teardown();
  }, [teardown]);

  /**
   * Open one microphone segment: a fresh getUserMedia + MediaRecorder pair,
   * good until the next pause or the final Stop.
   *
   * Plain mic recording is never mixed (a single raw track, see the class
   * doc), so unlike the tab/system path below there is no AudioContext
   * destination to route through and no reason to keep one alive across a
   * pause -- each segment gets its own, torn down the moment it stops. That
   * is what lets `pause()` release the device entirely rather than merely
   * suspending the encoder, which is what makes resuming on a *different*
   * device possible: there is nothing left holding the old one open.
   */
  const openMicSegment = useCallback(
    async (deviceId?: string) => {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: audioConstraints('mic', deviceId),
      });

      const ctx = new AudioContext();
      await ctx.resume();
      const node = ctx.createAnalyser();
      node.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(node);

      const segmentChunks: BlobPart[] = [];
      const media = new MediaRecorder(stream, { mimeType: mimeTypeRef.current });
      media.ondataavailable = (event) => {
        if (event.data.size > 0) segmentChunks.push(event.data);
      };
      media.onstop = () => {
        segments.current.push(new Blob(segmentChunks, { type: mimeTypeRef.current }));
        for (const track of stream.getTracks()) track.stop();
        node.disconnect();
        void ctx.close().catch(() => {});
        if (isFinalStop.current) finalizeRecording();
      };

      streams.current = [stream];
      audioCtx.current = ctx;
      analyser.current = node;
      recorder.current = media;
      setLiveStreams({ room: null, me: stream });
      media.start(TIMESLICE_MS);
    },
    [finalizeRecording],
  );

  const start = useCallback(
    async ({ source, deviceId, withMic }: StartOptions) => {
      setError(null);
      setPhase('starting');

      // Belt to the panel's braces. navigator.mediaDevices is absent outside a
      // secure context, and reaching for .getUserMedia on undefined is a
      // TypeError with no useful text in it.
      if (!navigator.mediaDevices?.getUserMedia) {
        setPhase('idle');
        setError(
          'This page cannot open a microphone: browsers only allow that over HTTPS or on ' +
            'localhost. Open the app over HTTPS, or upload a file instead.',
        );
        return;
      }

      const mimeType = pickMimeType();
      if (!mimeType) {
        setPhase('idle');
        setError('This browser cannot record audio. Chrome, Edge, Firefox and Safari 14.1+ can.');
        return;
      }

      if (source === 'mic') {
        segments.current = [];
        isFinalStop.current = false;
        accumulated.current = 0;
        startedWith.current = 'mic';
        mimeTypeRef.current = mimeType;
        startedAt.current = Date.now();
        // A plain mic capture has nothing to channel-split -- one source, one
        // speaker known already, same as a channel-separated recording's mic
        // channel, just without a room channel alongside it.
        channelMapRef.current = null;
        try {
          await openMicSegment(deviceId);
        } catch (err) {
          setPhase('idle');
          setError(explain(err));
          return;
        }
        setElapsedMs(0);
        setPhase('recording');
        ticker.current = setInterval(
          () => setElapsedMs(accumulated.current + (Date.now() - startedAt.current)),
          200,
        );
        raf.current = requestAnimationFrame(meter);
        wantsWakeLock.current = true;
        void acquireWakeLock();
        return;
      }

      const captured: MediaStream[] = [];
      try {
        const display = await navigator.mediaDevices.getDisplayMedia({
          // Audio-only display capture is not a thing: Chrome rejects a
          // request with no video, so we ask for the cheapest video we can.
          video: {
            displaySurface: source === 'tab' ? 'browser' : 'monitor',
            frameRate: 1,
          },
          audio: audioConstraints(source),
          // Hints, not guarantees; the user can still pick anything.
          ...({
            systemAudio: source === 'system' ? 'include' : 'exclude',
            selfBrowserSurface: 'exclude',
            surfaceSwitching: 'include',
          } as object),
        });
        captured.push(display);

        if (display.getAudioTracks().length === 0) {
          throw new Error(
            source === 'tab'
              ? 'That share has no audio. Pick a tab and tick “Also share tab audio”.'
              : 'That share has no audio. Choose “Entire screen” and tick “Also share system audio”.',
          );
        }

        if (withMic) {
          captured.push(
            await navigator.mediaDevices.getUserMedia({
              audio: audioConstraints('mic', deviceId),
            }),
          );
        }

        const tracks = captured.flatMap((stream) => stream.getAudioTracks());
        const mixing = tracks.length > 1;

        const ctx = new AudioContext();
        await ctx.resume();
        const destination = ctx.createMediaStreamDestination();
        const node = ctx.createAnalyser();
        node.fftSize = 1024;

        if (mixing) {
          // Two speakers, kept apart: channel 0 is whatever this capture
          // picks up ("the room"), channel 1 is the local mic. A merger
          // instead of connecting both straight to `destination` -- which
          // would sum them into one signal on every channel, the way this
          // used to work -- is what lets the backend diarize by channel
          // identity instead of guessing from voices (see
          // diarize_channels_file). Tradeoff: reviewing the clip before
          // upload now plays you in one ear and the room in the other,
          // instead of both blended into both.
          const merger = ctx.createChannelMerger(2);
          const [display, mic] = captured;
          const roomInput = ctx.createMediaStreamSource(display);
          roomInput.connect(node);
          roomInput.connect(merger, 0, 0);
          const meInput = ctx.createMediaStreamSource(mic);
          meInput.connect(node);
          meInput.connect(merger, 0, 1);
          merger.connect(destination);
          channelMapRef.current = 'mic_room';
          setLiveStreams({ room: display, me: mic });
        } else {
          for (const stream of captured) {
            if (stream.getAudioTracks().length === 0) continue;
            const input = ctx.createMediaStreamSource(stream);
            input.connect(node);
          }
          channelMapRef.current = null;
          setLiveStreams({ room: captured[0] ?? null, me: null });
        }

        // Audio-only, always: a display capture's stream carries the video
        // track too, and handing MediaRecorder a video track under an
        // audio/* mimeType is a NotSupportedError.
        const media = new MediaRecorder(
          mixing ? destination.stream : new MediaStream(tracks),
          { mimeType },
        );
        chunks.current = [];
        media.ondataavailable = (event) => {
          if (event.data.size > 0) chunks.current.push(event.data);
        };
        media.onstop = () => {
          const blob = new Blob(chunks.current, { type: mimeType });
          const at = new Date();
          const durationSec =
            (accumulated.current + (startedAt.current ? Date.now() - startedAt.current : 0)) / 1000;
          const file = new File([blob], recordingFilename(startedWith.current, mimeType, at), {
            type: mimeType,
            lastModified: at.getTime(),
          });
          const url = URL.createObjectURL(blob);
          clipUrl.current = url;
          setClip((previous) => {
            if (previous) URL.revokeObjectURL(previous.url);
            return { file, durationSec, url, channelMap: channelMapRef.current };
          });
          setPhase('done');
          teardown();
        };

        // The user pressing Chrome's own "Stop sharing" bar ends the tracks
        // without touching our UI. Treat it as pressing Stop, or the recording
        // sits there apparently running and records silence.
        for (const stream of captured) {
          for (const track of stream.getTracks()) {
            track.addEventListener('ended', () => {
              if (recorder.current?.state !== 'inactive') recorder.current?.stop();
            });
          }
        }

        streams.current = captured;
        audioCtx.current = ctx;
        analyser.current = node;
        recorder.current = media;
        startedWith.current = source;
        accumulated.current = 0;
        startedAt.current = Date.now();

        media.start(TIMESLICE_MS);
        setElapsedMs(0);
        setPhase('recording');
        ticker.current = setInterval(
          () => setElapsedMs(accumulated.current + (Date.now() - startedAt.current)),
          200,
        );
        raf.current = requestAnimationFrame(meter);
        wantsWakeLock.current = true;
        void acquireWakeLock();
      } catch (err) {
        for (const stream of captured) {
          for (const track of stream.getTracks()) track.stop();
        }
        setPhase('idle');
        setError(explain(err));
      }
    },
    [meter, teardown, acquireWakeLock, openMicSegment],
  );

  const pause = useCallback(() => {
    if (recorder.current?.state !== 'recording') return;
    accumulated.current += Date.now() - startedAt.current;
    startedAt.current = 0;
    if (ticker.current !== null) clearInterval(ticker.current);
    ticker.current = null;

    if (startedWith.current === 'mic') {
      // Release the microphone entirely rather than just suspending the
      // encoder: freeing the device is what makes resuming on a *different*
      // one possible. The finished segment is stitched back together with
      // whatever comes next at the final Stop.
      if (raf.current !== null) cancelAnimationFrame(raf.current);
      raf.current = null;
      setLevel(0);
      recorder.current.stop();
      recorder.current = null;
    } else {
      recorder.current.pause();
    }

    setElapsedMs(accumulated.current);
    setPhase('paused');
  }, []);

  const resume = useCallback(
    (options: { deviceId?: string } = {}) => {
      if (phase !== 'paused') return;

      if (startedWith.current !== 'mic') {
        if (recorder.current?.state !== 'paused') return;
        recorder.current.resume();
        startedAt.current = Date.now();
        ticker.current = setInterval(
          () => setElapsedMs(accumulated.current + (Date.now() - startedAt.current)),
          200,
        );
        setPhase('recording');
        return;
      }

      setError(null);
      setResuming(true);
      void (async () => {
        try {
          await openMicSegment(options.deviceId);
          startedAt.current = Date.now();
          ticker.current = setInterval(
            () => setElapsedMs(accumulated.current + (Date.now() - startedAt.current)),
            200,
          );
          raf.current = requestAnimationFrame(meter);
          setPhase('recording');
        } catch (err) {
          // Nothing was torn down -- the mic from before pause is already
          // gone, but the segments recorded so far are intact and Stop still
          // works. Stay paused so a different device can be tried.
          setError(explain(err));
        } finally {
          setResuming(false);
        }
      })();
    },
    [phase, openMicSegment, meter],
  );

  const stop = useCallback(() => {
    if (recorder.current && recorder.current.state !== 'inactive') {
      // onstop finishes the job: it needs the last ondataavailable first.
      isFinalStop.current = true;
      recorder.current.stop();
      return;
    }
    // Paused with the microphone already released (see pause() above): there
    // is no live recorder to stop, so build the file directly from whatever
    // segments already exist.
    if (segments.current.length > 0) finalizeRecording();
  }, [finalizeRecording]);

  const discard = useCallback(() => {
    clipUrl.current = null;
    segments.current = [];
    setClip((previous) => {
      if (previous) URL.revokeObjectURL(previous.url);
      return null;
    });
    setElapsedMs(0);
    setError(null);
    setPhase('idle');
  }, []);

  return {
    phase,
    error,
    elapsedMs,
    elapsed: fmtElapsedMs(elapsedMs),
    level,
    clip,
    liveStreams,
    start,
    pause,
    resume,
    resuming,
    stop,
    discard,
    busy: phase === 'starting',
    live: phase === 'recording' || phase === 'paused',
  };
}

/** Turn a getUserMedia/getDisplayMedia rejection into something actionable. */
function explain(err: unknown): string {
  if (!(err instanceof Error)) return 'Could not start recording';
  switch (err.name) {
    case 'NotAllowedError':
      return 'Permission refused. Allow microphone or screen access for this site and try again.';
    case 'NotFoundError':
      return 'No microphone found. Plug one in, or pick a different source.';
    case 'NotReadableError':
      return 'The microphone is in use by another app and could not be opened.';
    case 'AbortError':
      return 'The picker was dismissed before anything was shared.';
    case 'OverconstrainedError':
      return 'That input is no longer available. Pick another one.';
    default:
      return err.message || 'Could not start recording';
  }
}

/**
 * The audio inputs this browser can see.
 *
 * Labels are empty until the user has granted microphone permission once, so
 * the list is worth re-reading after the first successful start -- and on
 * `devicechange`, which is what fires when a headset is plugged in mid-setup.
 */
export function useAudioInputs(enabled: boolean) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [requesting, setRequesting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      setDevices(all.filter((d) => d.kind === 'audioinput'));
    } catch {
      setDevices([]);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void refresh();
    navigator.mediaDevices?.addEventListener?.('devicechange', refresh);
    return () => navigator.mediaDevices?.removeEventListener?.('devicechange', refresh);
  }, [enabled, refresh]);

  /**
   * Ask for the microphone purely to read real device names, not to record.
   *
   * `enumerateDevices` only fills in `label` once this origin's mic
   * permission is "granted" -- until then every input comes back blank,
   * indistinguishable except by count, which is what turns into "Input 1" in
   * the picker. A grant is sticky per origin, so this happens at most once:
   * the browser will not prompt again when the real recording starts.
   */
  const requestAccess = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) return;
    setRequesting(true);
    setRequestError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      for (const track of stream.getTracks()) track.stop();
      await refresh();
    } catch (err) {
      setRequestError(
        err instanceof Error && err.name === 'NotAllowedError'
          ? 'Permission refused. Allow microphone access for this site and try again.'
          : 'Could not read device names.',
      );
    } finally {
      setRequesting(false);
    }
  }, [refresh]);

  return { devices, refresh, requestAccess, requesting, requestError };
}
