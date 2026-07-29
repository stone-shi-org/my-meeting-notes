import { describe, expect, it } from 'vitest';
import {
  audioConstraints,
  blockedReason,
  detectPlatform,
  extensionFor,
  fmtElapsedMs,
  pickMimeType,
  recordingFilename,
  sourceSupport,
} from '../recording';

const UA = {
  chromeMac:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
  chromeWindows:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
  edgeWindows:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
  firefoxMac:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:140.0) Gecko/20100101 Firefox/140.0',
  safariMac:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
};

const SECURE = { hasDisplayMedia: true, hasRecorder: true, hasMediaDevices: true };

const support = (ua: string, hasDisplayMedia = true) =>
  sourceSupport(
    detectPlatform(ua, { hasDisplayMedia, hasRecorder: true, hasMediaDevices: true }),
  );

describe('detectPlatform', () => {
  it('does not mistake Chrome on a Mac for Safari', () => {
    const platform = detectPlatform(UA.chromeMac, SECURE);
    expect(platform.chromium).toBe(true);
    expect(platform.safari).toBe(false);
    expect(platform.mac).toBe(true);
  });

  it('recognises Safari, which advertises itself as everything', () => {
    const platform = detectPlatform(UA.safariMac, SECURE);
    expect(platform.safari).toBe(true);
    expect(platform.chromium).toBe(false);
  });

  it('recognises Edge as Chromium', () => {
    expect(detectPlatform(UA.edgeWindows, SECURE).chromium).toBe(true);
  });
});

describe('sourceSupport', () => {
  it('always offers the microphone', () => {
    for (const ua of Object.values(UA)) {
      expect(support(ua).mic.available).toBe(true);
    }
  });

  it('offers tab audio on Chromium, including on macOS', () => {
    expect(support(UA.chromeMac).tab.available).toBe(true);
    expect(support(UA.chromeWindows).tab.available).toBe(true);
  });

  it('refuses system audio on macOS, where the OS gives the browser none', () => {
    const mac = support(UA.chromeMac).system;
    expect(mac.available).toBe(false);
    expect(mac.hint).toMatch(/loopback/i);
  });

  it('offers system audio on Windows, where it works', () => {
    expect(support(UA.chromeWindows).system.available).toBe(true);
  });

  it('refuses both capture sources on Firefox and Safari', () => {
    for (const ua of [UA.firefoxMac, UA.safariMac]) {
      expect(support(ua).tab.available).toBe(false);
      expect(support(ua).system.available).toBe(false);
    }
    // Named, so the hint is actionable rather than "unsupported".
    expect(support(UA.firefoxMac).tab.hint).toMatch(/Firefox/);
    expect(support(UA.safariMac).tab.hint).toMatch(/Safari/);
  });

  it('refuses capture when the browser has no getDisplayMedia at all', () => {
    expect(support(UA.chromeWindows, false).tab.available).toBe(false);
  });
});

describe('an insecure origin', () => {
  // How this app is normally reached: http://192.168.x.x:4020. Browsers gate
  // navigator.mediaDevices on a secure context, so the whole object is missing
  // -- while MediaRecorder, which is not gated, is still there. Detecting on
  // MediaRecorder alone is what turned "cannot record here" into a TypeError.
  const lan = detectPlatform(UA.chromeWindows, {
    hasMediaDevices: false,
    hasDisplayMedia: false,
    hasRecorder: true,
  });

  it('is not mistaken for a working recorder', () => {
    expect(lan.hasRecorder).toBe(true);
    expect(lan.hasMediaDevices).toBe(false);
    expect(blockedReason(lan)).toMatch(/HTTPS or on localhost/);
  });

  it('offers no source at all, microphone included', () => {
    const support = sourceSupport(lan);
    for (const source of ['mic', 'tab', 'system'] as const) {
      expect(support[source].available).toBe(false);
      expect(support[source].hint).toMatch(/secure page/);
    }
  });

  it('reports no reason when everything is present', () => {
    expect(blockedReason(detectPlatform(UA.chromeWindows, SECURE))).toBeNull();
  });

  it('still reports the older "no MediaRecorder" case', () => {
    const ancient = detectPlatform(UA.safariMac, {
      hasMediaDevices: true,
      hasDisplayMedia: false,
      hasRecorder: false,
    });
    expect(blockedReason(ancient)).toMatch(/cannot record audio/);
  });
});

describe('pickMimeType', () => {
  it('prefers opus in webm', () => {
    expect(pickMimeType(() => true)).toBe('audio/webm;codecs=opus');
  });

  it('falls back to mp4 for Safari, which writes no webm', () => {
    const safari = (type: string) => type.startsWith('audio/mp4');
    expect(pickMimeType(safari)).toBe('audio/mp4;codecs=mp4a.40.2');
  });

  it('is null when nothing is supported, rather than an empty string', () => {
    expect(pickMimeType(() => false)).toBeNull();
  });
});

describe('extensionFor', () => {
  it('maps each container to an extension the upload route accepts', () => {
    expect(extensionFor('audio/webm;codecs=opus')).toBe('.webm');
    expect(extensionFor('audio/mp4;codecs=mp4a.40.2')).toBe('.m4a');
    expect(extensionFor('audio/ogg;codecs=opus')).toBe('.ogg');
  });

  it('falls back to webm for anything unrecognised', () => {
    expect(extensionFor('audio/weird')).toBe('.webm');
  });
});

describe('recordingFilename', () => {
  it('says what was recorded and when, and sorts by time', () => {
    const at = new Date(2026, 6, 28, 14, 32);
    expect(recordingFilename('tab', 'audio/webm;codecs=opus', at)).toBe(
      'recording-tab-audio-2026-07-28-1432.webm',
    );
    expect(recordingFilename('mic', 'audio/mp4', at)).toBe(
      'recording-microphone-2026-07-28-1432.m4a',
    );
  });

  it('zero-pads so names sort lexically', () => {
    const at = new Date(2026, 0, 5, 9, 7);
    expect(recordingFilename('system', 'audio/webm', at)).toContain('2026-01-05-0907');
  });
});

describe('audioConstraints', () => {
  it('leaves conference processing on for a microphone', () => {
    expect(audioConstraints('mic')).toMatchObject({
      echoCancellation: true,
      noiseSuppression: true,
    });
  });

  it('pins the exact device when one was chosen', () => {
    expect(audioConstraints('mic', 'device-7')).toMatchObject({
      deviceId: { exact: 'device-7' },
    });
  });

  it('turns processing off for captured audio, which it would mangle', () => {
    for (const source of ['tab', 'system'] as const) {
      expect(audioConstraints(source)).toMatchObject({
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      });
    }
  });
});

describe('fmtElapsedMs', () => {
  it('counts up in mm:ss and adds hours only when needed', () => {
    expect(fmtElapsedMs(0)).toBe('00:00');
    expect(fmtElapsedMs(9_000)).toBe('00:09');
    expect(fmtElapsedMs(724_000)).toBe('12:04');
    expect(fmtElapsedMs(3_787_000)).toBe('1:03:07');
  });
});
