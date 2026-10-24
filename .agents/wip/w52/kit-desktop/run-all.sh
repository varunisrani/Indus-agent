#!/bin/bash
# All captures against one prepared worktree: bash run-all.sh <worktree> [<out dir>]
# (prepare it first with prepare.sh). Each surface runs on its own; a failure is reported and the
# others still run.
set -uo pipefail
TOOLS=$(cd "$(dirname "$0")" && pwd)
WT=${1:?worktree}
OUT=${2:-/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/shots}
rc=0
for s in desktop web tui; do
  echo "== $s"
  bash "$TOOLS/run-$s.sh" "$WT" "$OUT" > "$TOOLS/$s.log" 2>&1 || { echo "$s FAILED (see $TOOLS/$s.log)"; rc=1; }
  grep -E '^(shot|note)' "$TOOLS/$s.log"
done
exit $rc
