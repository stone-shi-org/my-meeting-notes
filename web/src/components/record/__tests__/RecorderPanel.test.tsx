import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RecorderPanel } from '../RecorderPanel';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ settings: {} }), post: vi.fn() },
}));

vi.mock('@/lib/recording', async () => {
  const actual = await vi.importActual<typeof import('@/lib/recording')>('@/lib/recording');
  return {
    ...actual,
    detectPlatform: () => ({
      mac: false,
      firefox: false,
      safari: false,
      chromium: true,
      hasDisplayMedia: true,
      hasRecorder: true,
      hasMediaDevices: true,
    }),
    blockedReason: () => null,
    pickMimeType: () => 'audio/webm',
  };
});

interface FakeTrack {
  kind: 'audio' | 'video';
  enabled: boolean;
  stop: ReturnType<typeof vi.fn>;
  addEventListener: ReturnType<typeof vi.fn>;
}

function makeFakeTrack(kind: 'audio' | 'video' = 'audio'): FakeTrack {
  return {
    kind,
    enabled: true,
    stop: vi.fn(),
    addEventListener: vi.fn(),
  };
}

function makeFakeStream(tracks: FakeTrack[]): MediaStream {
  return {
    getTracks: () => tracks as unknown as MediaStreamTrack[],
    getAudioTracks: () =>
      tracks.filter((t) => t.kind === 'audio') as unknown as MediaStreamTrack[],
    getVideoTracks: () =>
      tracks.filter((t) => t.kind === 'video') as unknown as MediaStreamTrack[],
  } as unknown as MediaStream;
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static isTypeSupported = () => true;

  state: 'inactive' | 'recording' | 'paused' = 'inactive';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  stream: MediaStream;

  constructor(stream: MediaStream) {
    this.stream = stream;
    FakeMediaRecorder.instances.push(this);
  }

  start() {
    this.state = 'recording';
  }

  pause() {
    this.state = 'paused';
  }

  resume() {
    this.state = 'recording';
  }

  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: new Blob(['audio'], { type: 'audio/webm' }) });
    this.onstop?.();
  }
}

class FakeAudioContext {
  resume = vi.fn().mockResolvedValue(undefined);
  close = vi.fn().mockResolvedValue(undefined);
  createAnalyser = vi.fn(() => ({
    fftSize: 1024,
    getByteTimeDomainData: (arr: Uint8Array) => arr.fill(160),
    disconnect: vi.fn(),
  }));
  createMediaStreamSource = vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn(),
  }));
  createMediaStreamDestination = vi.fn(() => ({
    stream: makeFakeStream([makeFakeTrack('audio')]),
  }));
  createChannelMerger = vi.fn(() => ({
    connect: vi.fn(),
  }));
}

let micTracks: FakeTrack[] = [];
let roomTracks: FakeTrack[] = [];

beforeEach(() => {
  vi.clearAllMocks();
  FakeMediaRecorder.instances = [];
  micTracks = [];
  roomTracks = [];

  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  vi.stubGlobal(
    'MediaStream',
    class {
      private tracks: FakeTrack[];
      constructor(tracks: FakeTrack[] = []) {
        this.tracks = tracks;
      }
      getTracks() {
        return this.tracks;
      }
      getAudioTracks() {
        return this.tracks.filter((t) => t.kind === 'audio');
      }
    },
  );
  vi.stubGlobal('AudioContext', FakeAudioContext);
  if (!URL.createObjectURL) {
    URL.createObjectURL = vi.fn(() => 'blob:fake');
  } else {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:fake');
  }
  if (!URL.revokeObjectURL) {
    URL.revokeObjectURL = vi.fn();
  } else {
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
  }

  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      enumerateDevices: vi.fn().mockResolvedValue([]),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      getUserMedia: vi.fn(async () => {
        const track = makeFakeTrack('audio');
        micTracks.push(track);
        return makeFakeStream([track]);
      }),
      getDisplayMedia: vi.fn(async () => {
        const audio = makeFakeTrack('audio');
        const video = makeFakeTrack('video');
        roomTracks.push(audio);
        return makeFakeStream([audio, video]);
      }),
    },
  });
});

function renderRecorder(layout: 'compact' | 'wide' = 'wide') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onRecorded = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <RecorderPanel layout={layout} onRecorded={onRecorded} />
    </QueryClientProvider>,
  );
  return { onRecorded };
}

describe('RecorderPanel mute button', () => {
  it.each(['wide', 'compact'] as const)(
    'mutes and unmutes the microphone while keeping recording active in %s layout',
    async (layout) => {
      const user = userEvent.setup();
      renderRecorder(layout);

      await user.click(screen.getByRole('button', { name: /Start recording/i }));

      const muteBtn = await screen.findByRole('button', { name: /^Mute$/i });
      expect(muteBtn).toHaveAttribute('aria-pressed', 'false');
      expect(micTracks).toHaveLength(1);
      expect(micTracks[0].enabled).toBe(true);
      expect(FakeMediaRecorder.instances[0].state).toBe('recording');

      await user.click(muteBtn);

      const unmuteBtn = screen.getByRole('button', { name: /^Unmute$/i });
      expect(unmuteBtn).toHaveAttribute('aria-pressed', 'true');
      expect(micTracks[0].enabled).toBe(false);
      // Recording itself continues uninterrupted
      expect(FakeMediaRecorder.instances[0].state).toBe('recording');
      expect(screen.getByRole('status')).toHaveTextContent(/microphone muted/i);

      await user.click(unmuteBtn);

      expect(screen.getByRole('button', { name: /^Mute$/i })).toHaveAttribute(
        'aria-pressed',
        'false',
      );
      expect(micTracks[0].enabled).toBe(true);
      expect(FakeMediaRecorder.instances[0].state).toBe('recording');
    },
  );

  it('mutes only the microphone while room/tab capture keeps going', async () => {
    const user = userEvent.setup();
    renderRecorder('wide');

    await user.click(screen.getByRole('radio', { name: /Browser tab/i }));
    await user.click(screen.getByRole('button', { name: /Start recording/i }));

    const muteBtn = await screen.findByRole('button', { name: /^Mute$/i });
    expect(roomTracks).toHaveLength(1);
    expect(micTracks).toHaveLength(1);
    expect(roomTracks[0].enabled).toBe(true);
    expect(micTracks[0].enabled).toBe(true);

    await user.click(muteBtn);

    expect(screen.getByRole('button', { name: /^Unmute$/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    // Microphone is muted, but room track and recorder keep running
    expect(micTracks[0].enabled).toBe(false);
    expect(roomTracks[0].enabled).toBe(true);
    expect(FakeMediaRecorder.instances[0].state).toBe('recording');
  });

  it('preserves muted state across pause and resume on microphone recordings', async () => {
    const user = userEvent.setup();
    renderRecorder('compact');

    await user.click(screen.getByRole('button', { name: /Start recording/i }));
    await user.click(await screen.findByRole('button', { name: /^Mute$/i }));
    expect(micTracks[0].enabled).toBe(false);

    await user.click(screen.getByRole('button', { name: /^Pause$/i }));
    await user.click(screen.getByRole('button', { name: /^Resume$/i }));

    // Resumed segment opens a second mic track, which starts muted
    expect(micTracks).toHaveLength(2);
    expect(micTracks[1].enabled).toBe(false);
    expect(screen.getByRole('button', { name: /^Unmute$/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });
});
