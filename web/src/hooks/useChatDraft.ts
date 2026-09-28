import { useEffect, useState } from 'react';

/**
 * Persists the in-progress chat draft to sessionStorage so closing/collapsing the
 * panel or navigating away does not lose user input.
 *
 * Scoped by conversation key (e.g. `mmn:draft:thread:123`, `mmn:draft:home`, `mmn:draft:meeting:456`).
 */
export function useChatDraft(storageKey: string): [string, React.Dispatch<React.SetStateAction<string>>] {
  const [draft, setDraft] = useState<string>(() => {
    try {
      if (typeof window !== 'undefined' && window.sessionStorage) {
        return window.sessionStorage.getItem(storageKey) ?? '';
      }
    } catch {
      // Private browsing or quota restriction
    }
    return '';
  });

  useEffect(() => {
    try {
      if (typeof window !== 'undefined' && window.sessionStorage) {
        if (draft) {
          window.sessionStorage.setItem(storageKey, draft);
        } else {
          window.sessionStorage.removeItem(storageKey);
        }
      }
    } catch {
      // Private browsing or quota restriction
    }
  }, [storageKey, draft]);

  return [draft, setDraft];
}
