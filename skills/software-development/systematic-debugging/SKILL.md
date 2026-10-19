---
name: systematic-debugging
description: Reproduce, isolate, fix and verify a bug
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [debugging, bugs, root-cause, testing]
---

# Systematic debugging

Treat a bug as a question with an answer you can prove. Guessing and patching symptoms wastes the
user's time and often hides the real fault. Work in four stages and do not skip ahead.

## When to use

- Something fails, crashes, hangs or gives the wrong answer and the cause is not obvious.
- A first quick fix did not work.
- A test fails only sometimes, or only on one machine.

## Procedure

1. **Reproduce.** Get the exact command, input and environment that shows the fault, and run it
   yourself with `terminal`. Save the full error output. If you cannot make it happen, collect
   more facts (logs, versions, the user's steps) before changing any code. Ask with `clarify` when
   a detail only the user knows is missing.
2. **Shrink the case.** Cut the reproduction down until removing anything more makes the bug go
   away: fewer input rows, one request, one test. Write it down as a script or a failing test; it
   becomes your measuring stick.
3. **Isolate.** Form one hypothesis at a time and design a check that could prove it wrong.
   - Read the code on the failing path with `read_file`; find related code with `search_files`.
   - Add temporary logging or assertions close to where good data turns bad.
   - Bisect when the bug is new: `git bisect start`, mark a good and a bad commit, and let the
     shrunk case decide each step (`git bisect run <script>` when it is automatic).
   - Compare a working and a failing environment one difference at a time.
   Keep a short log of each hypothesis and its result in `todo_list` so you do not test the same
   idea twice.
4. **Fix the cause.** Change the smallest thing that removes the root cause, not the symptom. If
   the same mistake can happen elsewhere, look for its siblings with `search_files`.
5. **Verify.** Run the shrunk case: it must pass now and must have failed before. Then run the
   wider test suite. Remove temporary logging.
6. **Explain.** Tell the user what was wrong, why it happened, what you changed, and how you
   proved it.

## Pitfalls

- Changing several things at once: when it starts working you will not know which change counted.
- Trusting the error message's location blindly; the fault is often earlier, where bad state was
  created.
- "Fixing" a flaky test with retries or longer sleeps. Find the race or the shared state instead.
- Swallowing an exception to make the failure disappear.
- Stopping at the first plausible cause without a test that pins it.

## Verification

- A test or script reproduces the original failure on the old code and passes on the new code.
- The broader test suite still passes.
- No debug prints, temporary flags or commented-out code remain (`search_files` for them).
