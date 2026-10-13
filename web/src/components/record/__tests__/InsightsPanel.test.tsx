import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { InsightsPanel } from '@/components/record/InsightsPanel';
import {
  useInsights,
  type InsightActionItem,
  type InsightQuestion,
  type InsightTopic,
} from '@/hooks/useInsights';
import { api } from '@/lib/api';

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('@/hooks/useInsights', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useInsights')>();
  return {
    ...actual,
    useInsights: vi.fn(),
  };
});

interface MockInsightsState {
  topics: InsightTopic[];
  questions: InsightQuestion[];
  actionItems: InsightActionItem[];
  error: string | null;
  loading: boolean;
  clear: () => void;
}

let insightsState: MockInsightsState;

function setInsights(partial: Partial<MockInsightsState>) {
  insightsState = { ...insightsState, ...partial };
  vi.mocked(useInsights).mockReturnValue(insightsState);
}

function renderInsightsPanel(enabled = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <InsightsPanel captions={[]} enabled={enabled} />
    </QueryClientProvider>,
  );
}

/** Find the scrollable `<ul>` right inside a section heading's container. */
function sectionList(headingPrefix: string): HTMLUListElement {
  const heading = screen.getByRole('heading', { name: new RegExp(`^${headingPrefix}`, 'i') });
  const list = heading.parentElement?.querySelector('ul');
  if (!list) throw new Error(`No <ul> found under section "${headingPrefix}"`);
  return list;
}

function stubScrollHeight(el: HTMLElement, height: number) {
  Object.defineProperty(el, 'scrollHeight', {
    configurable: true,
    get: () => height,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();

  insightsState = {
    topics: [],
    questions: [],
    actionItems: [],
    error: null,
    loading: false,
    clear: vi.fn(),
  };
  vi.mocked(useInsights).mockReturnValue(insightsState);

  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/insight-types') {
      return Promise.resolve([{ slug: 'general', name: 'General Meeting', prompt: 'p' }]);
    }
    if (url === '/settings') {
      return Promise.resolve({
        settings: {
          insights_model: {
            value: 'test/insights-model',
            type: 'str',
            is_secret: false,
            overridden: false,
          },
          insights_interval_sec: {
            value: 30,
            type: 'int',
            is_secret: false,
            overridden: false,
          },
        },
      });
    }
    return Promise.resolve({});
  });
});

describe('InsightsPanel auto-scroll', () => {
  it('renders an Auto-scroll checkbox enabled by default and persists toggles to localStorage', async () => {
    renderInsightsPanel();

    const checkbox = screen.getByRole('checkbox', { name: /auto-scroll/i });
    expect(checkbox).toBeChecked();
    expect(localStorage.getItem('mmn.insightsAutoScroll')).toBe('1');

    fireEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();
    expect(localStorage.getItem('mmn.insightsAutoScroll')).toBe('0');
  });

  it('automatically scrolls all three insight areas to the latest items when auto-scroll is checked', async () => {
    setInsights({
      topics: [{ title: 'Topic 1', summary: 'Summary 1', current: true }],
      questions: [{ question: 'Question 1?', ai_answer_points: ['Point 1'], discussion: '' }],
      actionItems: [{ text: 'Action 1', owner: 'Alice' }],
    });

    const { rerender } = renderInsightsPanel();

    await waitFor(() => {
      expect(screen.getByText('Topic 1')).toBeInTheDocument();
    });

    const topicsUl = sectionList('Topics');
    const questionsUl = sectionList('Questions');
    const actionsUl = sectionList('Action items');

    stubScrollHeight(topicsUl, 320);
    stubScrollHeight(questionsUl, 410);
    stubScrollHeight(actionsUl, 260);
    topicsUl.scrollTop = 0;
    questionsUl.scrollTop = 0;
    actionsUl.scrollTop = 0;

    setInsights({
      topics: [
        { title: 'Topic 1', summary: 'Summary 1', current: false },
        { title: 'Topic 2', summary: 'Summary 2', current: true },
      ],
      questions: [
        { question: 'Question 1?', ai_answer_points: ['Point 1'], discussion: '' },
        { question: 'Question 2?', ai_answer_points: ['Point 2'], discussion: 'Discussed' },
      ],
      actionItems: [
        { text: 'Action 1', owner: 'Alice' },
        { text: 'Action 2', owner: 'Bob' },
      ],
    });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    rerender(
      <QueryClientProvider client={client}>
        <InsightsPanel captions={[]} enabled />
      </QueryClientProvider>,
    );

    expect(topicsUl.scrollTop).toBe(320);
    expect(questionsUl.scrollTop).toBe(410);
    expect(actionsUl.scrollTop).toBe(260);
  });

  it('does not auto-scroll when unchecked, and scrolls all three areas when re-checked', async () => {
    localStorage.setItem('mmn.insightsAutoScroll', '0');
    setInsights({
      topics: [{ title: 'Topic 1', summary: 'Summary 1', current: true }],
      questions: [{ question: 'Question 1?', ai_answer_points: [], discussion: '' }],
      actionItems: [{ text: 'Action 1', owner: null }],
    });

    const { rerender } = renderInsightsPanel();

    await waitFor(() => {
      expect(screen.getByText('Topic 1')).toBeInTheDocument();
    });

    const checkbox = screen.getByRole('checkbox', { name: /auto-scroll/i });
    expect(checkbox).not.toBeChecked();

    const topicsUl = sectionList('Topics');
    const questionsUl = sectionList('Questions');
    const actionsUl = sectionList('Action items');

    stubScrollHeight(topicsUl, 500);
    stubScrollHeight(questionsUl, 600);
    stubScrollHeight(actionsUl, 450);
    topicsUl.scrollTop = 20;
    questionsUl.scrollTop = 30;
    actionsUl.scrollTop = 10;

    setInsights({
      topics: [
        { title: 'Topic 1', summary: 'Summary 1', current: false },
        { title: 'Topic 2', summary: 'Summary 2', current: true },
      ],
      questions: [
        { question: 'Question 1?', ai_answer_points: [], discussion: '' },
        { question: 'Question 2?', ai_answer_points: [], discussion: '' },
      ],
      actionItems: [
        { text: 'Action 1', owner: null },
        { text: 'Action 2', owner: null },
      ],
    });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    rerender(
      <QueryClientProvider client={client}>
        <InsightsPanel captions={[]} enabled />
      </QueryClientProvider>,
    );

    // Preserves manual scroll positions while auto-scroll is off.
    expect(topicsUl.scrollTop).toBe(20);
    expect(questionsUl.scrollTop).toBe(30);
    expect(actionsUl.scrollTop).toBe(10);

    // Turning auto-scroll back on immediately jumps all three areas to the bottom.
    fireEvent.click(checkbox);
    expect(topicsUl.scrollTop).toBe(500);
    expect(questionsUl.scrollTop).toBe(600);
    expect(actionsUl.scrollTop).toBe(450);
  });

  it('renders all accumulated items in each scrollable section so earlier insights remain reachable', async () => {
    setInsights({
      topics: Array.from({ length: 7 }, (_, i) => ({
        title: `Topic ${i + 1}`,
        summary: `Summary ${i + 1}`,
        current: i === 6,
      })),
      questions: Array.from({ length: 7 }, (_, i) => ({
        question: `Question ${i + 1}?`,
        ai_answer_points: [],
        discussion: '',
      })),
      actionItems: Array.from({ length: 7 }, (_, i) => ({
        text: `Action ${i + 1}`,
        owner: null,
      })),
    });

    renderInsightsPanel();

    await waitFor(() => {
      expect(screen.getByText('Topic 1')).toBeInTheDocument();
    });

    expect(screen.getByText('Topic 7')).toBeInTheDocument();
    expect(screen.getByText('Question 1?')).toBeInTheDocument();
    expect(screen.getByText('Question 7?')).toBeInTheDocument();
    expect(screen.getByText('Action 1')).toBeInTheDocument();
    expect(screen.getByText('Action 7')).toBeInTheDocument();
  });
});
