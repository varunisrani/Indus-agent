# Getting work back in git

Git almost never loses a commit outright; it loses track of it. These recipes find it again.
Run each one through `terminal` and read the output before acting on it.

## The reflog is the safety net

`git reflog -n 30` lists every position `HEAD` held recently, newest first, with the command that
moved it. A commit that vanished after a reset, rebase or branch delete is usually in this list.
Once you have its id:

```bash
git branch rescue/<short-name> <sha>   # pin it to a branch first, then inspect it
git log --oneline -n 5 rescue/<short-name>
```

Pinning to a new branch is safe: it changes nothing else, and the user can decide what to keep.

## A rebase or merge went badly

| Situation | Command |
|---|---|
| Still in the middle of it | `git rebase --abort` or `git merge --abort` |
| Finished, but the result is wrong | find the pre-rebase tip with `git reflog`, then `git branch rescue/before-rebase <sha>` |
| Want the branch back where it was | confirm with the user, then `git reset --hard <sha>` |

## Committed on the wrong branch

1. Note the commit ids: `git log --oneline -n 3`.
2. Switch to (or create) the right branch and `git cherry-pick <sha>...` them across.
3. Only after the copies are safe, and with the user's agreement, remove them from the wrong
   branch with `git reset --hard <sha-before-them>`.

## Uncommitted changes that were discarded

`git restore`, `git checkout -- <file>` and `git reset --hard` drop uncommitted edits without a
trace in the reflog. Check for the editor's local history, a stash (`git stash list`), or a
dangling blob (`git fsck --lost-found`) only if the change was staged at some point. Tell the user
plainly when it cannot be recovered.

## Before any destructive step

- Make a pointer first: `git branch backup/<date>-<topic>`.
- Say what will change and wait for a yes.
