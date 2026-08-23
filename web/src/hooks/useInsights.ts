import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type { Caption } from './useLiveCaption';

export interface InsightTopic {
  title: string;
  summary: string;
  current: boolean;
}

export interface InsightQuestion {
  question: string;
  ai_answer_points: string[];
  /** LLM summary of what participants actually said in response to this
   * question, so far in the transcript -- "" if not addressed yet. Distinct
   * from ai_answer_points, which is the model's own suggestion, not a report
   * of what was actually discussed. */
  discussion: string;
}

export interface InsightActionItem {
  text: string;
  owner: string | null;
}

interface AnalyzeResponse {
  topics?: InsightTopic[];
  questions?: InsightQuestion[];
  action_items?: InsightActionItem[];
}

export const DEFAULT_INSIGHTS_INTERVAL_SEC = 30;

/** Always "Room"/"Me", regardless of LiveTranscriptPanel's cosmetic rename --
 * the prompt's SYSTEM section explains those two literal labels, and feeding
 * it a user-typed name instead would contradict what it was just told they
 * mean. */
function renderTranscript(captions: Caption[]): string {
  return captions.map((c) => `${c.channel === 'me' ? 'Me' : 'Room'}: ${c.text}`).join('\n');
}

/**
 * Periodic LLM analysis of the live-caption transcript -- see
 * app/routers/insights.py and app/services/insights.py.
 *
 * A plain interval rather than a websocket: unlike captions this is one
 * request/response per tick, not a stream, and a dropped tick is fine to
 * just retry next interval rather than needing a persistent connection to
 * recover.
 *
 * Stateless on the server by design (see insights.py's docstring), so this
 * hook is what actually holds the running state -- whatever analyze()
 * returned last call is sent back as `previous` on the next one, and the
 * server grows all three lists together. Switching meeting_type starts that
 * over: a topic/question/action-item list from the other prompt doesn't
 * mean anything here.
 *
 * Deliberately does NOT reset on `enabled` going false -- that is what
 * happens when the user clicks Stop, and the whole point of this feature is
 * that stopping must not throw away what was gathered (InsightsPanel keeps
 * rendering it after Stop). The only things that clear the accumulated
 * state are: `meetingType` changing (a topic list from the other prompt
 * means nothing here), `sessionKey` changing (bumped by the caller when a
 * genuinely new recording starts, e.g. "Record again" -- see
 * RecorderPanel), or calling the returned `clear()` directly (the panel's
 * manual "Clean" button).
 */
export function useInsights(
  captions: Caption[],
  meetingType: string,
  enabled: boolean,
  intervalSec: number = DEFAULT_INSIGHTS_INTERVAL_SEC,
  sessionKey: number = 0,
) {
  const [topics, setTopics] = useState<InsightTopic[]>([]);
  const [questions, setQuestions] = useState<InsightQuestion[]>([]);
  const [actionItems, setActionItems] = useState<InsightActionItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Refs so the interval's closure always reads the latest captions/previous
  // result without having to tear down and recreate the timer on every new
  // caption -- that would reset the "every X seconds" cadence to "X seconds
  // after the last word", never actually firing during a quiet stretch.
  const captionsRef = useRef(captions);
  captionsRef.current = captions;
  const previousRef = useRef<AnalyzeResponse | null>(null);
  const inFlight = useRef(false);

  // Stable identity (setState setters never change) so including it in the
  // effect below never causes an extra run -- it's there so a manual Clean
  // click and an automatic reset share exactly one implementation.
  const clear = useCallback(() => {
    setTopics([]);
    setQuestions([]);
    setActionItems([]);
    setError(null);
    previousRef.current = null;
  }, []);

  useEffect(() => {
    clear();
  }, [meetingType, sessionKey, clear]);

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;

    async function tick() {
      // A slow call overlapping the next tick would race two `previous`
      // reads against one one server-side answer -- skip rather than queue.
      if (inFlight.current) return;
      const transcript = renderTranscript(captionsRef.current);
      if (!transcript.trim()) return; // nothing said yet; don't spend a call on silence

      inFlight.current = true;
      setLoading(true);
      try {
        const result = await api.post<AnalyzeResponse>('/insights/analyze', {
          meeting_type: meetingType,
          transcript,
          previous: previousRef.current,
        });
        if (cancelled) return;
        previousRef.current = result;
        setError(null);
        setTopics(result.topics ?? []);
        setQuestions(result.questions ?? []);
        setActionItems(result.action_items ?? []);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Insights call failed');
      } finally {
        inFlight.current = false;
        if (!cancelled) setLoading(false);
      }
    }

    const ms = Math.max(5, intervalSec) * 1000;
    const id = window.setInterval(() => void tick(), ms);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [enabled, meetingType, intervalSec]);

  return { topics, questions, actionItems, error, loading, clear };
}
