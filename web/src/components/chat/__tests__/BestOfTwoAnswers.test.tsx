import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BestOfTwoAnswers } from '@/components/chat/BestOfTwoAnswers';
import { pickSecondModel } from '@/hooks/useChatModel';
import { api } from '@/lib/api';
import { streamChat, type ChatStreamHandlers } from '@/lib/chatStream';

vi.mock('@/lib/chatStream', () => ({ streamChat: vi.fn() }));
vi.mock('@/lib/api', () => ({ api: { del: vi.fn() } }));

interface Msg {
  id: number;
  content: string;
}

// One controllable stream per model: the test decides when each one speaks and ends.
const streams: Record<string, { h: ChatStreamHandlers<Msg>; end: () => void }> = {};

beforeEach(() => {
  vi.clearAllMocks();
  for (const k of Object.keys(streams)) delete streams[k];
  vi.mocked(api.del).mockResolvedValue(undefined as never);
  vi.mocked(streamChat).mockImplementation(((
    _path: string,
    _message: string,
    h: ChatStreamHandlers<Msg>,
    _signal: AbortSignal,
    model: string,
  ) =>
    new Promise<void>((resolve) => {
      streams[model] = { h, end: resolve };
    })) as never);
});

function setup() {
  const onChoose = vi.fn();
  const onSettled = vi.fn();
  render(
    <BestOfTwoAnswers<Msg>
      path="/home/chat"
      message="Q"
      models={['a', 'b']}
      modelName={(id) => `Model ${id.toUpperCase()}`}
      discardPath={(id) => `/home/chat/messages/${id}`}
      onChoose={onChoose}
      onSettled={onSettled}
    />,
  );
  return { onChoose, onSettled };
}

const speak = (model: string, id: number, text: string) =>
  act(() => {
    streams[model].h.onToken(text);
    streams[model].h.onDone({ id, content: text });
  });

describe('BestOfTwoAnswers', () => {
  it('sends the same prompt to both models and shows each answer under its own name', () => {
    setup();
    expect(vi.mocked(streamChat).mock.calls.map((c) => [c[1], c[4]])).toEqual([
      ['Q', 'a'],
      ['Q', 'b'],
    ]);
    act(() => streams.a.h.onToken('alpha answer'));
    act(() => streams.b.h.onToken('beta answer'));
    expect(screen.getByRole('region', { name: 'Answer from Model A' })).toHaveTextContent(
      'alpha answer',
    );
    expect(screen.getByRole('region', { name: 'Answer from Model B' })).toHaveTextContent(
      'beta answer',
    );
  });

  it('closing a finished side deletes it, keeps the other model, and settles on the survivor', async () => {
    const user = userEvent.setup();
    const { onChoose, onSettled } = setup();
    speak('a', 11, 'alpha');
    speak('b', 12, 'beta');

    await user.click(screen.getByRole('button', { name: 'Close Model A answer' }));

    expect(api.del).toHaveBeenCalledWith('/home/chat/messages/11');
    expect(onChoose).toHaveBeenCalledWith('b');
    // Back to a single column: the closed side is gone, no more close buttons.
    expect(screen.queryByRole('region', { name: 'Answer from Model A' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Close/ })).toBeNull();

    // The survivor's stream has not closed yet (follow-up chips still to come).
    expect(onSettled).not.toHaveBeenCalled();
    act(() => streams.b.h.onSuggestions?.(['next?']));
    await act(async () => streams.b.end());

    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(onSettled.mock.calls[0][0]).toMatchObject({
      message: { id: 12 },
      model: 'b',
      suggestions: ['next?'],
      error: null,
    });
  });

  it('closing a side that is still streaming aborts it instead of deleting anything', async () => {
    const user = userEvent.setup();
    const { onChoose } = setup();
    act(() => streams.a.h.onToken('partial'));

    await user.click(screen.getByRole('button', { name: 'Close Model A answer' }));

    expect(api.del).not.toHaveBeenCalled();
    expect(vi.mocked(streamChat).mock.calls[0][3].aborted).toBe(true);
    expect(vi.mocked(streamChat).mock.calls[1][3].aborted).toBe(false);
    expect(onChoose).toHaveBeenCalledWith('b');
  });

  it('settles with the error when the surviving side failed', async () => {
    const user = userEvent.setup();
    const { onSettled } = setup();
    speak('a', 11, 'alpha');
    act(() => streams.b.h.onError({ code: 'LLM_AUTH_FAILED', message: 'bad key' }));
    await act(async () => streams.b.end());

    await user.click(screen.getByRole('button', { name: 'Close Model A answer' }));

    expect(onSettled.mock.calls[0][0]).toMatchObject({
      message: null,
      error: { code: 'LLM_AUTH_FAILED' },
    });
  });
});

describe('pickSecondModel', () => {
  const ids = ['a', 'b', 'c'];
  it('honours a valid pick', () => expect(pickSecondModel(ids, 'a', 'c')).toBe('c'));
  it('never returns the primary model', () => {
    expect(pickSecondModel(ids, 'a', 'a')).toBe('b');
    expect(pickSecondModel(ids, 'b', null)).toBe('a');
  });
  it('drops a pick that is no longer enabled', () =>
    expect(pickSecondModel(ids, 'a', 'zzz')).toBe('b'));
  it('is null with a single model', () => expect(pickSecondModel(['a'], 'a', null)).toBeNull());
});
