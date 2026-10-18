#!/usr/bin/env bash
# agent-sync.sh — commit only the named paths and push them, while builders keep writing other files
# in the same checkout.
#
# Usage: agent-sync.sh "<commit message>" <path>...
#   e.g. agent-sync.sh "[Agent 1] status — Agent 1 usage 42% — step 17/39" \
#          .agents/agent-1.json .agents/usage-log-agent-1.md
# Env: BRANCH         branch to push; must be the checked-out branch (default: main)
#      REMOTE         default origin
#      RETRIES        push attempts (default 5)
#      TRAILER        text appended after a blank line, e.g. your commit attribution lines
#      REFRESH_GLOBS  space-separated globs of coordination files that are safe to refresh from HEAD
#                     after a replay (default ".agents/* HANDOFF.md")
#
# 1. Stages and commits only <path>...; every other file in the working tree is left as it is.
# 2. Pushes. If the push is rejected because someone else pushed meanwhile, replays this branch's new
#    commits on top of the remote in a throwaway worktree, moves the branch there (the working tree is
#    not touched) and pushes again. No `--autostash` in the shared checkout: it would carry the
#    builders' half-written files away mid-edit. Never force-pushes.
# 3. After a replay, coordination files the remote changed (REFRESH_GLOBS, and only if you had not
#    edited them) are refreshed from HEAD. For any other file it prints "origin changed <file>": check
#    that file out from HEAD once no builder edits it, and never commit your older copy over it.
#
# Exit: 0 pushed | 1 bad usage, replay conflict, or push failed after retries (your commits stay on
#       the local branch; resolve by hand)
set -euo pipefail

if [ $# -lt 2 ]; then
  sed -n '2,25p' "$0"
  exit 1
fi
MSG="$1"; shift
CURRENT=$(git rev-parse --abbrev-ref HEAD)
BR="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
RETRIES="${RETRIES:-5}"
REFRESH_GLOBS="${REFRESH_GLOBS:-.agents/* HANDOFF.md}"
if [ "$BR" != "$CURRENT" ]; then
  echo "BRANCH=$BR but $CURRENT is checked out; switch first" >&2
  exit 1
fi
if [ -n "${TRAILER:-}" ]; then MSG="$MSG"$'\n\n'"$TRAILER"; fi

# 1. Commit only the named paths (relative to the current directory).
PATHS=()
for p in "$@"; do
  if [ -e "$p" ] || git ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
    git add -A -- "$p"
    PATHS+=("$p")
  else
    echo "skipped $p: neither on disk nor tracked" >&2
  fi
done
if [ ${#PATHS[@]} -eq 0 ] || git diff --cached --quiet -- "${PATHS[@]}"; then
  echo "nothing to commit in the named paths; pushing the commits already on $BR" >&2
else
  git commit -q -m "$MSG" -- "${PATHS[@]}"
fi

# Everything below works with repository-relative paths.
cd "$(git rev-parse --show-toplevel)"
UPSTREAM="refs/remotes/$REMOTE/$BR"

is_refreshable() {
  local g
  set -f   # the globs are patterns for `case`, not for the shell
  for g in $REFRESH_GLOBS; do
    # shellcheck disable=SC2254
    case "$1" in $g) set +f; return 0 ;; esac
  done
  set +f
  return 1
}

# 2. Push, replaying on the remote when it moved.
for attempt in $(seq 1 "$RETRIES"); do
  if git push -q "$REMOTE" "$BR"; then
    git log --oneline -1
    exit 0
  fi
  sleep $(( 2 ** attempt ))
  git fetch -q "$REMOTE" "+refs/heads/$BR:$UPSTREAM" || continue
  if git merge-base --is-ancestor "$UPSTREAM" "$BR"; then
    continue   # the remote did not move: network trouble, try again
  fi

  TMP=$(mktemp -d "${TMPDIR:-/tmp}/agent-sync.XXXXXX")
  git worktree add -q --detach "$TMP" "$UPSTREAM"
  if ! git -C "$TMP" cherry-pick "$UPSTREAM..$BR" >/dev/null; then
    git -C "$TMP" cherry-pick --abort >/dev/null 2>&1 || true
    git worktree remove --force "$TMP" || { rm -rf "$TMP"; git worktree prune; }
    echo "replaying $BR on $REMOTE/$BR conflicts with what was pushed there; your commits are intact on $BR, resolve by hand" >&2
    exit 1
  fi
  NEW=$(git -C "$TMP" rev-parse HEAD)
  git worktree remove --force "$TMP" || { rm -rf "$TMP"; git worktree prune; }

  OLD=$(git rev-parse "$BR")
  CHANGED=$(git diff --name-only "$OLD" "$NEW")
  # Decide per file before HEAD moves: refresh only coordination files you had not edited locally.
  REFRESH=(); WARN=()
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if is_refreshable "$f" && git diff --quiet "$OLD" -- "$f" 2>/dev/null; then
      REFRESH+=("$f")
    else
      WARN+=("$f")
    fi
  done <<< "$CHANGED"

  git update-ref "refs/heads/$BR" "$NEW" "$OLD"
  git reset -q   # the index follows the new HEAD; the working tree (builders' files) is untouched
  for f in ${REFRESH[@]+"${REFRESH[@]}"}; do
    git checkout -q HEAD -- "$f" 2>/dev/null || rm -f -- "$f"
  done
  for f in ${WARN[@]+"${WARN[@]}"}; do
    echo "origin changed $f; check it out from HEAD once no builder edits it" >&2
  done
done
echo "push to $REMOTE/$BR failed after $RETRIES attempts" >&2
exit 1
