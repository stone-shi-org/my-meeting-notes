import { describe, expect, it } from 'vitest';
import { parseSseFrames } from '../chatStream';

describe('parseSseFrames', () => {
  it('parses a single complete frame', () => {
    const { frames, rest } = parseSseFrames('event: token\ndata: {"text":"hi"}\n\n');
    expect(frames).toEqual([{ event: 'token', data: '{"text":"hi"}' }]);
    expect(rest).toBe('');
  });

  it('parses multiple frames from one buffer', () => {
    const buffer =
      'event: token\ndata: {"text":"a"}\n\n' +
      'event: token\ndata: {"text":"b"}\n\n' +
      'event: done\ndata: {"id":1}\n\n';
    const { frames, rest } = parseSseFrames(buffer);
    expect(frames.map((f) => f.event)).toEqual(['token', 'token', 'done']);
    expect(rest).toBe('');
  });

  it('holds back an incomplete trailing frame as rest', () => {
    const { frames, rest } = parseSseFrames(
      'event: token\ndata: {"text":"a"}\n\nevent: token\ndata: {"text":"b"',
    );
    expect(frames).toEqual([{ event: 'token', data: '{"text":"a"}' }]);
    expect(rest).toBe('event: token\ndata: {"text":"b"');
  });

  it('resumes correctly once the rest of a split frame arrives', () => {
    const first = parseSseFrames('event: token\ndata: {"tex');
    expect(first.frames).toEqual([]);
    const second = parseSseFrames(first.rest + 't":"hi"}\n\n');
    expect(second.frames).toEqual([{ event: 'token', data: '{"text":"hi"}' }]);
  });

  it('skips comment (keepalive) lines', () => {
    const { frames } = parseSseFrames(': keepalive\n\nevent: done\ndata: {"id":1}\n\n');
    expect(frames).toEqual([{ event: 'done', data: '{"id":1}' }]);
  });

  it('defaults to a "message" event when no event line is present', () => {
    const { frames } = parseSseFrames('data: {"text":"hi"}\n\n');
    expect(frames).toEqual([{ event: 'message', data: '{"text":"hi"}' }]);
  });

  it('returns nothing for an empty buffer', () => {
    expect(parseSseFrames('')).toEqual({ frames: [], rest: '' });
  });
});
