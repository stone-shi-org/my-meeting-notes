import { Select } from '@/components/ui/primitives';
import type { useChatModel } from '@/hooks/useChatModel';

/**
 * The model selector in every chat panel header, plus the "Best of 2" switch.
 * With the switch on, a second selector appears and the next send goes to both
 * models. Hidden entirely when only one model is enabled -- there is nothing to
 * choose between.
 */
export function ChatModelPicker({
  chatModel,
  disabled = false,
}: {
  chatModel: ReturnType<typeof useChatModel>;
  /** Locked while a compare is on screen: changing models mid-answer means nothing. */
  disabled?: boolean;
}) {
  if (chatModel.options.length < 2) return null;

  const { options, selected, second, bestOf2 } = chatModel;

  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex items-center gap-2">
        <Select
          aria-label={bestOf2 ? 'First chat model' : 'Chat model'}
          className="h-7 min-w-0 flex-1 text-xs"
          value={selected ?? ''}
          disabled={disabled}
          onChange={(e) => chatModel.setModel(e.target.value)}
        >
          {options.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.name}
            </option>
          ))}
        </Select>
        <label className="flex shrink-0 items-center gap-1.5 text-xs text-fg-muted">
          <input
            type="checkbox"
            checked={bestOf2}
            disabled={disabled}
            onChange={(e) => chatModel.setBestOf2(e.target.checked)}
          />
          Best of 2
        </label>
      </div>
      {bestOf2 && (
        <Select
          aria-label="Second chat model"
          className="h-7 text-xs"
          value={second ?? ''}
          disabled={disabled}
          onChange={(e) => chatModel.setSecond(e.target.value)}
        >
          {options
            .filter((opt) => opt.id !== selected)
            .map((opt) => (
              <option key={opt.id} value={opt.id}>
                {opt.name}
              </option>
            ))}
        </Select>
      )}
    </div>
  );
}
