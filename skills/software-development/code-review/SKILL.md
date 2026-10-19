---
name: code-review
description: Review a change for correctness, risk and clarity
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [review, diff, quality, risk]
---

# Code review

Read a change the way a careful teammate would: first ask whether it does what it claims, then
whether it can hurt anyone, and only then how it reads. Report findings the author can act on.

## When to use

- The user asks you to review a diff, a branch, a pull request or a set of files.
- You finished your own change and want a second pass before handing it back.
- The user asks "is this safe to merge?" or "what could go wrong here?".

## Procedure

1. **Get the whole change.** For a local branch, `terminal`: `git diff --stat <base>...HEAD` then
   `git diff <base>...HEAD`. For a pull request, see the `github-cli` skill (`gh pr diff`). Note
   how many files and lines move; a very large change deserves a plan with `todo_list`.
2. **Learn the intent.** Read the description, the linked issue and the commit messages. Write one
   sentence for yourself: "this change should …". Every finding is measured against it.
3. **Read the surrounding code, not just the hunks.** Use `read_file` on each touched file and
   `search_files` for callers of anything whose signature or behaviour changed.
4. **Correctness pass.** For each hunk ask:
   - What inputs reach this code, and what happens at the edges (empty, missing, huge, negative,
     unicode, concurrent)?
   - Are errors handled where they can occur, and do they leave state consistent?
   - Do the tests exercise the new branches? Would they fail without the change?
5. **Risk pass.** Look for security and operational harm: untrusted input reaching a shell, SQL,
   a file path or HTML; secrets in code or logs; missing permission checks; migrations that cannot
   be undone; changes to public interfaces; new dependencies and their licences; performance cliffs
   inside loops.
6. **Clarity pass.** Names that mislead, dead code, comments that repeat the code, duplicated
   logic that should be shared. Keep these short; they matter less than the first two passes.
7. **Run what you can.** If a test command is known, run it with `terminal`. A claim backed by a
   failing test or a reproduced bug is worth more than a guess.
8. **Write the review.** Group findings by severity:
   - **Must fix**: wrong behaviour, data loss, security holes, broken builds.
   - **Should fix**: likely bugs, missing tests, risky patterns.
   - **Consider**: style and readability suggestions.
   For each, give the file and line, what is wrong, why it matters and a concrete suggestion.
   Finish with a one-line verdict.

## Pitfalls

- Do not review only the diff lines: most real bugs sit where changed code meets unchanged code.
- Do not pad the review with style nits to look thorough; three real findings beat thirty trivial
  ones.
- Say when you are unsure. "This may race with X; I could not confirm" is honest and useful.
- Do not rewrite the author's change unless asked. Suggest; let them decide.
- Never post comments to a shared system (a pull request, a chat) without the user's go-ahead.

## Verification

- Every "must fix" names a location and explains a concrete failure.
- You ran the tests or said clearly that you could not.
- The verdict matches the findings (no "looks good" above a must-fix list).
