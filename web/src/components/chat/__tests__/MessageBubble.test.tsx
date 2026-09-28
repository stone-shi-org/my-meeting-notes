import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { MessageBubble } from '@/components/chat/MessageBubble';
import * as clipboard from '@/lib/clipboard';

vi.mock('@/lib/clipboard', () => ({
  copyText: vi.fn(),
}));

describe('MessageBubble', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('user turn', () => {
    it('renders the user message prompt text', () => {
      render(<MessageBubble role="user" content="Summarize recent threads" />);
      expect(screen.getByText('Summarize recent threads')).toBeInTheDocument();
    });

    it('renders a copy button with copy prompt label', () => {
      render(<MessageBubble role="user" content="Hello world prompt" />);
      const copyBtn = screen.getByRole('button', { name: /copy prompt/i });
      expect(copyBtn).toBeInTheDocument();
      expect(copyBtn).toHaveTextContent('Copy');
    });

    it('copies the prompt text and shows Copied feedback on click', async () => {
      const user = userEvent.setup();
      vi.mocked(clipboard.copyText).mockResolvedValue(true);

      render(<MessageBubble role="user" content="My question" />);
      const copyBtn = screen.getByRole('button', { name: /copy prompt/i });

      await user.click(copyBtn);

      expect(clipboard.copyText).toHaveBeenCalledWith('My question');
      expect(await screen.findByText('Copied')).toBeInTheDocument();
    });

    it('shows error notice if copying fails', async () => {
      const user = userEvent.setup();
      vi.mocked(clipboard.copyText).mockResolvedValue(false);

      render(<MessageBubble role="user" content="Blocked question" />);
      const copyBtn = screen.getByRole('button', { name: /copy prompt/i });

      await user.click(copyBtn);

      expect(clipboard.copyText).toHaveBeenCalledWith('Blocked question');
      expect(await screen.findByText(/clipboard blocked/i)).toBeInTheDocument();
    });
  });

  describe('assistant turn', () => {
    it('renders markdown response', () => {
      render(<MessageBubble role="assistant" content="**Bold summary**" />);
      expect(screen.getByText('Bold summary')).toBeInTheDocument();
    });

    it('renders streaming indicator when isStreaming is true', () => {
      const { container } = render(
        <MessageBubble role="assistant" content="Streaming text" isStreaming />
      );
      const prose = container.querySelector('.answer-streaming');
      expect(prose).not.toBeNull();
    });

    it('enhances code blocks with language badge and copy button', () => {
      const { container } = render(
        <MessageBubble role="assistant" content={`\`\`\`python\nprint('hello')\n\`\`\``} />
      );
      const codeWrapper = container.querySelector('.code-block-container');
      expect(codeWrapper).not.toBeNull();
      const lang = container.querySelector('.code-block-lang');
      expect(lang?.textContent).toBe('python');
      const copyBtn = container.querySelector('.code-copy-btn');
      expect(copyBtn).not.toBeNull();
    });
  });
});

