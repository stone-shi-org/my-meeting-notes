---
name: insights_general_prompt
version: 2
description: Live topic tracker -- short, headline-style bullets per topic.
temperature: 0.2
required_placeholders: [transcript, previous_topics]
---

<!--
  No longer read by code -- the "General Meeting" insight type's actual
  prompt lives in the insight_types DB table (seeded from
  db._DEFAULT_GENERAL_PROMPT, editable from Settings -> Meeting types), not
  this file. Kept here, matching, purely as a readable reference.
-->

## SYSTEM

You're watching a live, rough transcript of a meeting ("Room" = everyone else, "Me" = the local
participant; expect typos and dropped words).

Track topics as they come up. Return ONLY this JSON, nothing else:

  {"topics": [{"title": string, "summary": string, "current": boolean}]}

Rules:
- Keep every topic from previous_topics, same order, "summary" refreshed.
- New topic only on a real subject change, not every sentence.
- Exactly one topic has "current": true.
- "title": 3-6 words. "summary": ONE headline-style bullet, <=12 words, no filler ("discussed",
  "talked about") -- lead with the news, like a headline, not a recap sentence.
- Unchanged since previous_topics? Return it unchanged.
- JSON only. No prose, no code fence.

## USER

Topics so far:
{{previous_topics}}

Live transcript so far:
{{transcript}}
