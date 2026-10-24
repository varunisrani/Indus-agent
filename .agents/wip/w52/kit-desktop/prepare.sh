#!/bin/bash
# Prepare a worktree for the capture scripts: bash prepare.sh <worktree>
#   1. .venv            uv sync --locked --offline (shared uv cache)
#   2. node_modules     pnpm install --frozen-lockfile --offline (store pnpm-store-p20, then the
#                       sandbox store under $S/home); nothing is fetched
#   3. bundles          apps/web/dist and apps/tui/dist with vite directly (never `pnpm run`: pnpm
#                       12 re-verifies and re-installs dependencies before a run, over the network)
#   4. Electron 44.5.1  fetch-electron.sh (the one network step, through the proxy, SHA-256 checked)
set -euo pipefail
export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/compare/work-i-desktop
export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus
unset TMPDIR
SP=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad
TOOLS=$(cd "$(dirname "$0")" && pwd)
WT=$(cd "${1:?worktree}" && pwd)
cd "$WT"

echo "== uv sync"
uv sync --locked --offline 2>&1 | tail -2

echo "== pnpm install (offline)"
if ! pnpm install --frozen-lockfile --offline --store-dir "$SP/pnpm-store-p20" 2>&1 | tail -4; then :; fi
if [ ! -e node_modules/.modules.yaml ] || [ ! -d apps/web/node_modules ]; then
  pnpm install --frozen-lockfile --offline --store-dir "$S/home/.local/share/pnpm/store" 2>&1 | tail -4
fi
[ -d apps/web/node_modules ] && [ -d apps/desktop/node_modules ] && [ -d apps/tui/node_modules ] \
  || { echo "offline install failed (missing metadata?)"; exit 3; }

echo "== build apps/web"
(cd apps/web && ../../node_modules/.bin/vite build --logLevel warn)
echo "== build apps/tui"
(cd apps/tui && ../../node_modules/.bin/vite build --ssr src/entry.tsx --outDir dist --emptyOutDir --logLevel warn)

echo "== electron"
bash "$TOOLS/fetch-electron.sh" "$WT"
echo "prepared $WT"
