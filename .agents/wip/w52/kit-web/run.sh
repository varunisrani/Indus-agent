#!/bin/bash
# Indus web dashboard, every page/component in light and dark (compare brief, I-web):
#   bash run.sh <worktree> [<out dir>]      (SCRIPT=explore.ts for a one-pass survey of every route)
# The worktree needs: .venv (uv sync --locked --offline), node_modules (cp -al from a prepared tree),
# apps/web/dist and apps/tui/dist (node_modules/.bin/vite build, see the report). Stack temp dirs live
# under $S/t and are removed afterwards.
set -euo pipefail
export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/compare/work-i-web
export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus
mkdir -p $S/t; export TMPDIR=$S/t
TOOLS=$(cd "$(dirname "$0")" && pwd)
export WT=${1:?worktree}
export OUT=${2:-/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/compare/indus/web}
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers NO_PROXY=127.0.0.1,127.0.0.2,localhost no_proxy=127.0.0.1,127.0.0.2,localhost
# provider/agent variables never reach the stack
for v in $(env | grep -oE '^(INDUS_|OPENAI_|ANTHROPIC_|OPENROUTER_|GEMINI_|AZURE_|AWS_|GH_|GITHUB_|CLAUDE)[A-Za-z0-9_]*' || true); do
  [ "$v" = INDUS_HOME ] || unset "$v"
done
before=$(ls -d $TMPDIR/indus-shots-* 2>/dev/null | sort || true)
status=0
node "$TOOLS/${SCRIPT:-capture.ts}" || status=$?
after=$(ls -d $TMPDIR/indus-shots-* 2>/dev/null | sort || true)
comm -13 <(echo "$before") <(echo "$after") | while read -r d; do [ -n "$d" ] && rm -rf "$d"; done
exit $status
