---
name: git-workflow
description: Branch, commit, rebase and settle merge conflicts
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [git, version-control, branches, conflicts]
    requires_toolsets: [terminal]
---

# Git workflow

Keep the user's history tidy and their work safe. Every step below runs through `terminal`; read
the output of each command before you choose the next one.

## When to use

- The user asks you to start a piece of work on its own branch, commit it, or bring a branch up to
  date with its base.
- A merge, rebase or cherry-pick stopped with conflicts.
- The history needs a clean-up before it is shared (squash fixups, reword a message, split a
  commit).
- Something went wrong (a lost commit, a bad reset) and the work must be found again.

## Procedure

1. **Look before you touch.** Run `git status --short --branch` and `git log --oneline -n 10`.
   Note the current branch, whether it tracks a remote, and whether the tree is clean. If there are
   changes you did not make, ask the user what to do with them before going on.
2. **Branch from a fresh base.** `git fetch --prune`, then
   `git switch -c <topic> origin/<base>`. Name the branch after the change (`fix/login-timeout`,
   `docs/install-steps`), not after yourself.
3. **Commit in small, reviewable steps.** Stage on purpose: `git add -p` or named paths, never a
   blind `git add .` in a repo with build output or secrets nearby. Check `git diff --cached`
   before every commit. Write the subject in the imperative, under about 70 characters, and use the
   body to say *why* the change was needed.
4. **Follow the repo's own rules.** Read `CONTRIBUTING*`, `AGENTS.md` or the PR template with
   `read_file` if they exist: commit prefixes, sign-off lines and branch names there beat the
   defaults here.
5. **Update the branch.** Prefer `git rebase origin/<base>` for a private branch and
   `git merge origin/<base>` for one other people already pulled. Ask when you cannot tell which.
6. **Settle conflicts one file at a time.**
   - `git status` lists the files; `git diff` shows both sides.
   - Open each file with `read_file`, understand what each side meant, and write the combined
     result with `patch`. Do not just keep "ours" or "theirs" unless one side is plainly obsolete.
   - Remove every conflict marker, run the project's tests or at least a build, then
     `git add <file>` and `git rebase --continue` (or `git commit` for a merge).
   - If the conflict is about intent rather than text, stop and ask the user.
7. **Tidy before sharing.** `git rebase -i` is interactive and will hang a non-interactive
   terminal; use `git commit --fixup=<sha>` plus
   `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash origin/<base>` instead.
8. **Push.** First push: `git push -u origin <topic>`. After rewriting a pushed branch use
   `git push --force-with-lease`, and only on a branch that is yours.

## Pitfalls

- Never force-push, reset hard, or delete a branch the user did not ask you to rewrite. `main`,
  `master` and release branches are off limits unless the user says otherwise in this session.
- `git stash` hides work in a place people forget. Prefer a work-in-progress commit on the branch.
- `git checkout -- <file>` and `git restore` throw changes away with no undo. Confirm first.
- A rebase rewrites commit ids: anyone who pulled the old ones must be told.
- Line-ending or file-mode noise (`old mode 100644`) is usually a config issue, not a change; check
  `core.autocrlf` and `core.fileMode` before committing it.
- Large binary or generated files belong in `.gitignore`, not in a commit.

## Verification

- `git status` is clean, or holds only what the user expects.
- `git log --oneline origin/<base>..HEAD` lists exactly the commits you meant to make.
- No file contains `<<<<<<<`, `=======` or `>>>>>>>` left from a conflict
  (`search_files` for the markers).
- The tests or build that cover the touched code pass after the last rebase or merge.

## Quick reference

See `references/recovery.md` for getting lost work back (reflog, aborted rebases, a commit made on
the wrong branch).
