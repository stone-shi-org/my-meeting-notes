/**
 * Suggested next questions, generated from the question/answer pair that
 * just finished. Shared by ThreadChatPanel and TranscriptChatPanel, same
 * reason MessageBubble is: the chip look and click behavior must stay
 * identical between the two, and only the digest behind the suggestions
 * differs.
 */
export function FollowUpChips({
  suggestions,
  onPick,
  disabled,
}: {
  suggestions: string[];
  onPick: (text: string) => void;
  disabled?: boolean;
}) {
  if (suggestions.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2">
      {suggestions.map((prompt) => (
        <button
          key={prompt}
          type="button"
          disabled={disabled}
          onClick={() => onPick(prompt)}
          className="rounded-full border border-border bg-transparent px-3 py-1 text-sm text-fg-faint transition-colors duration-fast hover:border-border-strong hover:bg-surface hover:text-fg disabled:opacity-50"
        >
          {prompt}
        </button>
      ))}
    </div>
  );
}
