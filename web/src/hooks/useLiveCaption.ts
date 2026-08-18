import { useEffect, useState } from 'react';
import type { LiveStreams } from './useRecorder';

export interface Caption {
  channel: 'me' | 'room';
  text: string;
  at: number;
}

/** Mirrors app/routers/live_caption.py's channel_worker states, pushed over
 * the same socket as `{"type": "status", channel, state}` -- purely for the
 * recorder UI's activity dot next to each level meter. */
export type ActivityState = 'idle' | 'buffering' | 'calling' | 'reading';

const DEFAULT_ACTIVITY: Record<'me' | 'room', ActivityState> = { me: 'idle', room: 'idle' };

const SAMPLE_RATE = 16000;
// Native render quantum is 128 samples (~2.7 ms at 16 kHz) -- far too small
// to send one websocket message per callback. Batch to a quarter second: a
// live feel, without flooding the socket.
const FLUSH_SAMPLES = SAMPLE_RATE * 0.25;
// Generous rather than tuned tight: the wide recorder layout's transcript
// panel and Insights (useInsights) both read this same array as the whole
// session's transcript so far, not just a rolling display window like the
// original small caption strip needed. 2000 lines covers many hours at this
// hook's ~3-8s-per-caption cadence, comfortably more than any realistic
// single recording, while still bounding memory for a recording nobody
// remembered to stop.
const MAX_CAPTIONS = 2000;

/**
 * Live, disposable captions during an active recording.
 *
 * Deliberately separate from useRecorder: it taps the same MediaStreams
 * useRecorder exposes via `liveStreams` with its own AudioContexts, rather
 * than reusing useRecorder's own graph, so a bug here -- or the websocket
 * dropping -- cannot touch what MediaRecorder is writing to disk. Nothing
 * produced here is ever uploaded or saved; it exists only to render on
 * screen while the meeting is still happening. See app/routers/live_caption.py
 * for the other end of the socket.
 *
 * `language` (ISO-639-1, or '' for auto-detect) is always sent explicitly,
 * even when empty -- that is what lets an explicit "Auto-detect" pick in
 * the recorder UI override a non-empty Settings-page default for just this
 * recording, rather than the empty string being indistinguishable from
 * "the caller didn't pass one." See live_caption_ws's query-param handling.
 */
export function useLiveCaption(streams: LiveStreams, enabled: boolean, language: string) {
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [connected, setConnected] = useState(false);
  const [activity, setActivity] = useState(DEFAULT_ACTIVITY);

  useEffect(() => {
    if (!enabled || (!streams.room && !streams.me)) {
      setConnected(false);
      setActivity(DEFAULT_ACTIVITY);
      return;
    }

    let cancelled = false;
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const qs = new URLSearchParams({ language });
    const ws = new WebSocket(`${proto}://${window.location.host}/api/live-caption/ws?${qs}`);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      if (!cancelled) setConnected(true);
    };
    ws.onclose = () => {
      if (!cancelled) {
        setConnected(false);
        setActivity(DEFAULT_ACTIVITY);
      }
    };
    ws.onerror = () => {
      if (!cancelled) setConnected(false);
    };
    ws.onmessage = (event) => {
      if (cancelled || typeof event.data !== 'string') return;
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === 'caption' && (msg.channel === 'me' || msg.channel === 'room')) {
          setCaptions((prev) =>
            [...prev, { channel: msg.channel, text: String(msg.text ?? ''), at: Date.now() }].slice(
              -MAX_CAPTIONS,
            ),
          );
        } else if (
          msg?.type === 'status' &&
          (msg.channel === 'me' || msg.channel === 'room') &&
          ['idle', 'buffering', 'calling', 'reading'].includes(msg.state)
        ) {
          setActivity((prev) => ({ ...prev, [msg.channel]: msg.state as ActivityState }));
        }
      } catch {
        // A malformed message is not worth surfacing -- this is a disposable
        // UI feature, and the next message will probably be fine.
      }
    };

    const contexts: AudioContext[] = [];
    const cleanupFns: Array<() => void> = [];

    async function tap(stream: MediaStream | null, channelTag: 0 | 1) {
      if (!stream || stream.getAudioTracks().length === 0) return;

      let ctx: AudioContext;
      try {
        ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      } catch {
        ctx = new AudioContext();
      }
      if (ctx.sampleRate !== SAMPLE_RATE) {
        // Sending audio at the wrong rate would decode as gibberish, and
        // that is worse than no caption at all -- skip this channel rather
        // than guess at a resample.
        console.warn(
          `Live captions: this browser would not open a ${SAMPLE_RATE} Hz context ` +
            `(got ${ctx.sampleRate} Hz); skipping this channel.`,
        );
        void ctx.close().catch(() => {});
        return;
      }
      contexts.push(ctx);

      await ctx.audioWorklet.addModule(new URL('../lib/pcmWorklet.js', import.meta.url));
      if (cancelled) return;

      const source = ctx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(ctx, 'pcm-capture');
      source.connect(node);

      let buffered: Float32Array[] = [];
      let bufferedSamples = 0;

      node.port.onmessage = (event: MessageEvent<Float32Array>) => {
        buffered.push(event.data);
        bufferedSamples += event.data.length;
        if (bufferedSamples < FLUSH_SAMPLES) return;

        const merged = new Float32Array(bufferedSamples);
        let offset = 0;
        for (const chunk of buffered) {
          merged.set(chunk, offset);
          offset += chunk.length;
        }
        buffered = [];
        bufferedSamples = 0;

        const pcm16 = new Int16Array(merged.length);
        for (let i = 0; i < merged.length; i++) {
          const clamped = Math.max(-1, Math.min(1, merged[i]));
          pcm16[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
        }

        // One tag byte, then raw little-endian int16 mono -- see
        // live_caption.py's receive loop.
        const frame = new Uint8Array(1 + pcm16.byteLength);
        frame[0] = channelTag;
        frame.set(new Uint8Array(pcm16.buffer), 1);
        if (ws.readyState === WebSocket.OPEN) ws.send(frame);
      };

      cleanupFns.push(() => {
        node.port.onmessage = null;
        node.disconnect();
        source.disconnect();
      });
    }

    void tap(streams.room, 0);
    void tap(streams.me, 1);

    return () => {
      cancelled = true;
      for (const fn of cleanupFns) fn();
      for (const ctx of contexts) void ctx.close().catch(() => {});
      ws.close();
    };
    // streams.room/streams.me change identity exactly once per recording
    // (see useRecorder's setLiveStreams calls), so this effect re-runs at
    // start and stop, not on every render. language is included on
    // principle -- the recorder UI disables that control while live, so in
    // practice it never changes mid-connection.
  }, [streams.room, streams.me, enabled, language]);

  return { captions, connected, activity };
}
