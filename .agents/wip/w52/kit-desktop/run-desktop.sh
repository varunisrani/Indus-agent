#!/bin/bash
# Desktop screenshots: bash run-desktop.sh <worktree> [<out dir>]
# Needs: the worktree's node_modules (pnpm offline install), its .venv (uv sync --locked) and the
# Electron 44.5.1 binary unpacked by fetch-electron.sh.
set -euo pipefail
export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/compare/work-i-desktop
export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus
unset TMPDIR
TOOLS=$(cd "$(dirname "$0")" && pwd)
export WT=${1:?worktree}
export OUT=${2:-/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/shots}
before=$(ls -d /tmp/indus-e2e-* /tmp/indus-shots-* /tmp/indus-m-* 2>/dev/null | sort || true)
status=0
xvfb-run -a -s "-screen 0 1920x1080x24" node "$TOOLS/desktop.ts" || status=$?
after=$(ls -d /tmp/indus-e2e-* /tmp/indus-shots-* /tmp/indus-m-* 2>/dev/null | sort || true)
# remove only the temp folders this run created
comm -13 <(echo "$before") <(echo "$after") | while read -r d; do [ -n "$d" ] && rm -rf "$d"; done
exit $status
