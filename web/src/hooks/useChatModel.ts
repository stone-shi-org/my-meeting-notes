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

  return { options, selected, setModel };
}
