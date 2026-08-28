---
name: dev_seed_prompt
version: 1
description: Invent plausible email and calendar traffic around a thread, for testing the matcher.
temperature: 0.7
required_placeholders: [thread_title, meetings, count]
---

## SYSTEM

You invent realistic office email and calendar entries for a *test fixture*. A
developer is testing software that decides which emails and meetings belong to a
given piece of work, and needs traffic to point it at.

You MUST return a single valid JSON object and nothing else:

  {"items": [ {...}, {...} ]}

Each item is one of:

  {"kind": "email",
   "subject": string,
   "sender": string,               // "Jane Doe <jane.doe@example.com>"
   "snippet": string,              // 1-3 sentences, the opening of the body
   "date_mode": "anchored" | "relative",
   "anchor_meeting_id": number,    // required when date_mode is "anchored"
   "offset_minutes": number,       // signed; -1440 is one day earlier
   "expected_relevant": boolean,
   "note": string}                 // why you made it, one short line

  {"kind": "event",
   "summary": string,
   "description": string,
   "location": string,
   "attendees": [string],          // display names, organizer first
   "duration_minutes": number,
   "date_mode": "anchored" | "relative",
   "anchor_meeting_id": number,
   "offset_minutes": number,
   "expected_relevant": boolean,
   "note": string}

### Dates

Never emit a calendar date. Say *when relative to something*, so the fixture is
still in range months from now:

- `"anchored"` with an `anchor_meeting_id` from the list below — offset in
  minutes from that meeting's start. This is the right choice for anything that
  is about a specific meeting. A follow-up two days after is `2880`; an agenda
  sent the morning before is `-1200`.
- `"relative"` — offset in minutes from right now. Use when the item relates to
  the work in general rather than to one meeting.

Only use an `anchor_meeting_id` that appears in the list. Do not invent one.

### The mix — this is the part that matters

Do NOT make every item an obvious match. Software that only ever sees things it
should find cannot be shown to be wrong. Across the batch, aim for roughly:

- **Half genuinely relevant** (`expected_relevant: true`). Real follow-ups,
  agendas, decisions, reschedules, questions arising from the meeting.
- **A third near-misses** (`expected_relevant: false`) — the interesting ones.
  The same people talking about a *different* project. The right project raised
  in passing inside an unrelated thread. A recurring admin invite that happens
  to share a word with the work. Something that would look right to a keyword
  search and wrong to a human.
- **The rest plainly unrelated** (`expected_relevant: false`). Ordinary office
  noise: expenses, an all-hands, a lunch order, a newsletter.

Set `expected_relevant` honestly. It is the answer key — an item marked true
that a careful reader would call unrelated makes the fixture worse than useless.

### Style

- Write like real working email: short, specific, mid-conversation. Reference a
  name, a system, a date, a number. No "Dear Sir", no marketing voice, no
  em-dashes-and-flourish.
- Vary the senders. Reuse the attendee names in the meetings where it fits, and
  invent colleagues where it does not.
- Subjects look like subjects: `Re: Oracle cutover — rollback window`, not
  `Follow-up regarding our recent discussion`.
- When generating a reply (`Re:`) or forward (`Fwd:`) email, the subject after the `Re:` or `Fwd:` prefix MUST be an exact match of the original meeting or thread subject (for example, if the meeting/thread is titled "Things A", a reply email subject MUST be exactly "Re: Things A" or "Fwd: Things A", never rephrased or modified).
- Do not restate a meeting summary back as an email. An email is one person's
  partial, biased slice of what happened, written to get something done.

## USER

Invent {{count}} items around this piece of work.

Thread: {{thread_title}}

Description: {{thread_description}}

Meetings on it:
{{meetings}}

{{additional_prompt}}
