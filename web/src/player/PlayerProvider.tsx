import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import { buildIndex, findIndex, type TranscriptIndex } from '@/lib/transcriptIndex';
import { usePlayerStore } from './playerStore';
import type { Segment } from '@/types/api';

interface PlayerApi {
  audioRef: React.RefObject<HTMLAudioElement>;
  seek: (seconds: number, play?: boolean) => void;
  toggle: () => void;
  nudge: (delta: number) => void;
  setRate: (rate: number) => void;
}

const PlayerContext = createContext<PlayerApi | null>(null);

// 10 Hz. `timeupdate` alone fires ~4x/s and jitters, which reads as a broken
// highlight; a rAF loop throttled to 100ms tracks the audio smoothly.
const TICK_MS = 100;

export function PlayerProvider({
  src,
  segments,
  children,
}: {
  src: string;
  segments: Segment[];
  children: React.ReactNode;
}) {
  const audioRef = useRef<HTMLAudioElement>(null!);
  const rafRef = useRef<number | null>(null);
  const lastTick = useRef(0);
  const hint = useRef(-1);

  const index = useMemo<TranscriptIndex>(() => buildIndex(segments), [segments]);
  const set = usePlayerStore((s) => s.set);

  const sync = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const t = audio.currentTime;
    const next = findIndex(index, t, hint.current);
    hint.current = next;
    set({ currentTime: t, activeIndex: next });
  }, [index, set]);

  const loop = useCallback(() => {
    const now = performance.now();
    if (now - lastTick.current >= TICK_MS) {
      lastTick.current = now;
      sync();
    }
    rafRef.current = requestAnimationFrame(loop);
  }, [sync]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onPlay = () => {
      set({ playing: true });
      if (rafRef.current === null) rafRef.current = requestAnimationFrame(loop);
    };
    const onPause = () => {
      set({ playing: false });
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      sync();
    };
    const onLoaded = () => set({ duration: audio.duration || 0 });
    const onRate = () => set({ rate: audio.playbackRate });

    audio.addEventListener('play', onPlay);
    audio.addEventListener('pause', onPause);
    audio.addEventListener('ended', onPause);
    // Safari throttles rAF in background tabs, so keep these as a fallback.
    audio.addEventListener('timeupdate', sync);
    audio.addEventListener('seeked', sync);
    audio.addEventListener('loadedmetadata', onLoaded);
    audio.addEventListener('ratechange', onRate);

    return () => {
      audio.removeEventListener('play', onPlay);
      audio.removeEventListener('pause', onPause);
      audio.removeEventListener('ended', onPause);
      audio.removeEventListener('timeupdate', sync);
      audio.removeEventListener('seeked', sync);
      audio.removeEventListener('loadedmetadata', onLoaded);
      audio.removeEventListener('ratechange', onRate);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [loop, sync, set]);

  const seek = useCallback((seconds: number, play = false) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Math.max(0, seconds);
    if (play && audio.paused) void audio.play();
  }, []);

  const toggle = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) void audio.play();
    else audio.pause();
  }, []);

  const nudge = useCallback((delta: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + delta));
  }, []);

  const setRate = useCallback((rate: number) => {
    const audio = audioRef.current;
    if (audio) audio.playbackRate = rate;
  }, []);

  const api = useMemo<PlayerApi>(
    () => ({ audioRef, seek, toggle, nudge, setRate }),
    [seek, toggle, nudge, setRate],
  );

  return (
    <PlayerContext.Provider value={api}>
      {/* A real <audio> element, not a fake. Range support on the backend is
          what makes seeking work at all. */}
      <audio ref={audioRef} src={src} preload="metadata" className="sr-only" />
      {children}
    </PlayerContext.Provider>
  );
}

export function usePlayer(): PlayerApi {
  const api = useContext(PlayerContext);
  if (!api) throw new Error('usePlayer must be used inside PlayerProvider');
  return api;
}
