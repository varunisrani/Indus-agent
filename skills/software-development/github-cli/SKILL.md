---
name: github-cli
description: Work with GitHub issues and pull requests via gh
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [github, gh, issues, pull-requests, ci]
    requires_toolsets: [terminal]
---

# GitHub from the command line

The `gh` CLI reads and changes GitHub issues, pull requests and workflow runs from the terminal.
Reading is always fine; anything that other people will see (comments, reviews, merges, labels)
needs the user's go-ahead first.

## When to use

- The user mentions an issue or pull request by number or link.
- You need a PR's diff, review comments or CI status to do your work.
- The user asks you to open a pull request, file an issue or reply to a review.

## Procedure

1. **Check the tool and the login.** `gh --version` and `gh auth status` through `terminal`. If
   `gh` is missing or signed out, tell the user how to fix it (`gh auth login` is interactive, so
   they run it themselves) instead of trying to work around it.
2. **Know which repository you are in.** Inside a clone `gh` infers it; elsewhere pass
   `--repo owner/name`. Confirm with `gh repo view --json nameWithOwner`.
3. **Read before you act.**
   - Issue: `gh issue view <n> --comments`.
   - Pull request: `gh pr view <n> --comments`, `gh pr diff <n>`, `gh pr checks <n>`.
   - Lists: `gh issue list --state open --limit 20`, `gh pr list --author @me`.
   - For scripts and exact fields add `--json <fields>` and filter with `--jq`, rather than
     scraping the human output.
4. **CI failures.** `gh run list --branch <branch> --limit 5`, then `gh run view <id> --log-failed`
   to see only the failing steps. Fix locally (see `systematic-debugging`) and push; do not re-run
   a red job hoping it turns green unless the failure is clearly unrelated and the user agrees.
5. **Open a pull request.** Push the branch first (see `git-workflow`). Draft the title and body
   and show them to the user. Then `gh pr create --base <base> --title "…" --body-file <file>`;
   writing the body to a file with `write_file` avoids shell-quoting trouble. Follow the repo's PR
   template if one exists under `.github/`.
6. **Comment or review** only after the user approves the exact text:
   `gh pr comment <n> --body-file <file>`, `gh pr review <n> --comment --body-file <file>`.
7. **Anything else** has a subcommand or `gh api`: `gh api repos/{owner}/{repo}/…` returns JSON
   for the REST endpoints `gh` does not wrap.

## Pitfalls

- `gh` commands that open an editor or a browser (`--web`, missing `--body`) hang a
  non-interactive terminal. Always pass the text explicitly.
- Merging, closing, deleting branches and editing other people's comments are hard to undo. Ask
  first, every time.
- `gh` output can be long; prefer `--json` with only the fields you need.
- Tokens: never echo `GH_TOKEN` or `gh auth token` output into the conversation or a file.

## Verification

- After creating or changing something, read it back (`gh pr view <n>`, `gh issue view <n>`) and
  give the user the link.
- For CI work, `gh pr checks <n>` shows the new run passing, or you explain why it still fails.
