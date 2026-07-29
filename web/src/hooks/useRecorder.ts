import { useCallback, useEffect, useRef, useState } from 'react';
import {
  audioConstraints,
  fmtElapsedMs,
  pickMimeType,
  recordingFilename,
  type Source,
} from '@/lib/recording';

export type Phase = 'idle' | 'starting' | 'recording' | 'paused' | 'done';

export interface Clip {
  file: File;
  /** Wall-clock length of the recording, which the WebM header will not carry. */
  durationSec: number;
  /** Object URL for the review player. Revoked when the clip is replaced. */
  url: string;
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
 */
export function useRecorder() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [level, setLevel] = useState(0);
  const [clip, setClip] = useState<Clip | null>(null);

  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<BlobPart[]>([]);
  const streams = useRef<MediaStream[]>([]);
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

  const teardown = useCallback(() => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
    raf.current = null;
    if (ticker.current !== null) clearInterval(ticker.current);
    ticker.current = null;

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
  }, []);

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

  const start = useCallback(
    async ({ source, deviceId, withMic }: StartOptions) => {
      setError(null);
      setPhase('starting');

      const mimeType = pickMimeType();
      if (!mimeType) {
        setPhase('idle');
        setError('This browser cannot record audio. Chrome, Edge, Firefox and Safari 14.1+ can.');
        return;
      }

      const captured: MediaStream[] = [];
      try {
        if (source === 'mic') {
          captured.push(
            await navigator.mediaDevices.getUserMedia({
              audio: audioConstraints('mic', deviceId),
            }),
          );
        } else {
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
        }

        const tracks = captured.flatMap((stream) => stream.getAudioTracks());
        const mixing = tracks.length > 1;

        const ctx = new AudioContext();
        await ctx.resume();
        const destination = ctx.createMediaStreamDestination();
        const node = ctx.createAnalyser();
        node.fftSize = 1024;

        for (const stream of captured) {
          if (stream.getAudioTracks().length === 0) continue;
          const input = ctx.createMediaStreamSource(stream);
          if (mixing) input.connect(destination);
          input.connect(node);
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
            return { file, durationSec, url };
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
      } catch (err) {
        for (const stream of captured) {
          for (const track of stream.getTracks()) track.stop();
        }
        setPhase('idle');
        setError(explain(err));
      }
    },
    [meter, teardown],
  );

  const pause = useCallback(() => {
    if (recorder.current?.state !== 'recording') return;
    recorder.current.pause();
    accumulated.current += Date.now() - startedAt.current;
    startedAt.current = 0;
    if (ticker.current !== null) clearInterval(ticker.current);
    ticker.current = null;
    setElapsedMs(accumulated.current);
    setPhase('paused');
  }, []);

  const resume = useCallback(() => {
    if (recorder.current?.state !== 'paused') return;
    recorder.current.resume();
    startedAt.current = Date.now();
    ticker.current = setInterval(
      () => setElapsedMs(accumulated.current + (Date.now() - startedAt.current)),
      200,
    );
    setPhase('recording');
  }, []);

  const stop = useCallback(() => {
    if (!recorder.current || recorder.current.state === 'inactive') return;
    // onstop finishes the job: it needs the last ondataavailable first.
    recorder.current.stop();
  }, []);

  const discard = useCallback(() => {
    clipUrl.current = null;
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
    start,
    pause,
    resume,
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

  return { devices, refresh };
}
