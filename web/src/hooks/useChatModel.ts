import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '@/lib/api';

const LAST_MODEL_KEY = 'mmn.lastChatModel';

export interface ChatModelOption {
  id: string;
  name: string;
}

/**
 * Admin-approved chat models (Settings -> LLM), plus this browser's last pick.
 * Shared by HomeChatPanel, ThreadChatPanel and TranscriptChatPanel so "last used"
 * means the same thing in all three. The configured default (options[0], see
 * llm_svc.enabled_chat_models) wins until something else is chosen, and again
 * if the stored choice is later disabled by an admin.
 */
export function useChatModel() {
  const models = useQuery({
    queryKey: ['chat-models'],
    queryFn: () =>
      api.get<{ models: string[]; options?: ChatModelOption[] }>('/llm/chat-models'),
    staleTime: 300_000,
  });

  const [lastPicked, setLastPicked] = useState<string | null>(() =>
    localStorage.getItem(LAST_MODEL_KEY),
  );

  function setModel(next: string) {
    setLastPicked(next);
    localStorage.setItem(LAST_MODEL_KEY, next);
  }

  const modelIds = models.data?.models ?? [];
  const options: ChatModelOption[] =
    models.data?.options ?? modelIds.map((id) => ({ id, name: id }));
  const selected = (lastPicked && modelIds.includes(lastPicked) ? lastPicked : modelIds[0]) ?? null;

  // Best of 2: per panel and per visit, not remembered -- it doubles the cost of
  // every send, so it should not still be on tomorrow because it was on once.
  const [bestOf2, setBestOf2] = useState(false);
  const [pickedSecond, setPickedSecond] = useState<string | null>(null);
  const second = pickSecondModel(modelIds, selected, pickedSecond);

  return {
    options,
    selected,
    setModel,
    bestOf2: bestOf2 && modelIds.length > 1,
    setBestOf2,
    second,
    setSecond: setPickedSecond,
    /** Both models of a Best-of-2 send, or null when it is off / not possible. */
    pair: bestOf2 && selected && second ? ([selected, second] as [string, string]) : null,
  };
}

/**
 * The second Best-of-2 model: the user's pick if it is still valid, else the first
 * enabled model that is not the primary. Never equal to `primary` -- comparing a
 * model with itself costs two calls to learn nothing.
 */
export function pickSecondModel(
  modelIds: string[],
  primary: string | null,
  picked: string | null,
): string | null {
  if (picked && picked !== primary && modelIds.includes(picked)) return picked;
  return modelIds.find((id) => id !== primary) ?? null;
}
