---
name: meeting-notes-to-actions
description: Turn meeting notes into decisions and owned actions
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: productivity
    tags: [meetings, notes, actions, decisions]
---

# Meeting notes to actions

Raw notes and transcripts bury what matters. Pull out what was decided, who will do what by when,
and what is still open, so the people who were there (and those who were not) can act on it.

## When to use

- The user pastes or points to meeting notes, a transcript, or a chat thread and asks for a
  summary, minutes or action items.
- The user asks "what did we agree?" or "who owns what?" about a discussion.

## Procedure

1. **Read everything once** before writing. With a file, use `read_file`; with a long transcript,
   work through it in sections and keep a running list in `todo_list`.
2. **Separate four kinds of content:**
   - *Decisions*: things the group settled. Quote the deciding phrase when it is short.
   - *Actions*: a task with an owner and, ideally, a due date.
   - *Open questions*: raised but not settled, or parked for later.
   - *Context*: background that explains the above; keep only what a reader needs.
3. **Make every action concrete.** Start with a verb, name one owner, give a date. If the notes do
   not say who or when, mark it `owner: unassigned` or `due: not set` rather than guessing.
4. **Keep names and numbers exact.** Copy people's names, figures and dates as written; flag ones
   that look inconsistent instead of fixing them silently.
5. **Write the result** in this order: a two-line summary, decisions, actions (as a table:
   action, owner, due), open questions, then optional notes. Save it with `write_file` if the user
   wants a file.
6. **Offer follow-ups** without doing them unasked: drafting the follow-up email (see
   `writing-clearly`), or scheduling reminders for due dates (see `scheduling-jobs`).

## Pitfalls

- Turning every remark into an action. If nobody committed to it, it is a question or a note.
- Assigning owners or deadlines that were never agreed.
- Losing dissent: if someone disagreed with a decision, record it briefly.
- Including sensitive side remarks (personal matters, private opinions) that were not meant for a
  written record. When unsure, leave them out and mention that you did.

## Verification

- Each action has a verb, exactly one owner (or "unassigned") and a due date (or "not set").
- Each decision can be traced to a line in the notes.
- Nothing in the summary contradicts the source.
