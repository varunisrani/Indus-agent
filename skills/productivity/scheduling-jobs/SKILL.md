---
name: scheduling-jobs
description: Turn a request into a safe scheduled job
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: productivity
    tags: [cron, schedule, automation, reminders]
---

# Scheduling jobs

A scheduled job runs later, without the user watching. It must be clear about what it does, when
it runs, where its result goes, and how to stop it. Use the `cronjob_manage` tool; if it is not
available in this session, tell the user how to schedule it with `indus cron` instead.

## When to use

- The user asks for something to happen later or repeatedly: "every morning", "remind me on
  Friday", "check the build every 30 minutes".
- The user wants to change, pause, run now or remove an existing job.

## Procedure

1. **Pin down the four facts.** Ask with `clarify` for any that are missing:
   - *What*: the task, written as a complete instruction a fresh session can follow with no memory
     of this conversation (names, paths, URLs, the expected output).
   - *When*: a one-off time or a recurrence. Plain phrases work ("weekdays at 9am",
     "every 2h"); a five-field cron expression is fine for anything unusual. Confirm the time zone.
   - *Where* the result goes: back to this chat, a local file, or a configured messaging channel.
   - *How long*: forever, a number of runs, or until a date.
2. **Write the job prompt.** Self-contained, specific and short. Say what to do when there is
   nothing new (stay quiet rather than send an empty report). Attach a skill to the job when one
   already describes the work.
3. **Check for an existing job** with `cronjob_manage` `list`, so you update rather than duplicate.
4. **Show the plan** to the user: name, schedule in words, next run time, delivery target, prompt.
   Create it only after they agree.
5. **Create** with `cronjob_manage` `create`, then read it back with `list` and tell the user its
   id, its next run and how to pause or remove it.
6. **Offer a test run** with `run` when the job's output matters, so the user sees a real result
   before the first scheduled one.

## Pitfalls

- Prompts that rely on this conversation ("do the same as before"): the job will not have it.
- Too-frequent schedules that burn tokens or hammer a service. Suggest the slowest interval that
  still meets the need.
- Jobs that need approval for risky commands will be denied when nobody is watching; design the
  task so it does not need them, or keep it read-only.
- Creating a job just because a task mentions "daily" in passing. Only schedule what the user
  asked to schedule.
- Time zones and daylight-saving changes: state the zone and give the next run in local time.

## Verification

- `cronjob_manage` `list` shows the job with the agreed schedule, delivery target and state.
- The next run time, in the user's zone, is what they expect.
- If a test run was made, its output reached the agreed destination.
