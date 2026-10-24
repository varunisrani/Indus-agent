#!/bin/bash
# Remove build output and caches made for the captures (keeps the PNGs and these scripts):
#   bash cleanup.sh <worktree> [--all]   (--all also drops the sandbox HOME with the pnpm
#   metadata cache and store that the next offline install needs: use it after the last pass)
set -uo pipefail
S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/compare/work-i-desktop
WT=$(cd "${1:?worktree}" && pwd)
bash "$(dirname "$0")/fetch-electron.sh" "$WT" --remove || true
rm -rf "$WT/apps/web/dist" "$WT/apps/tui/dist" "$WT/apps/web/test-results"
rm -rf "$WT/node_modules" "$WT"/apps/*/node_modules "$WT"/packages/*/node_modules "$WT"/apps/desktop/e2e/node_modules
rm -rf "$WT/.venv"
rm -rf /tmp/indus-shots
if [ "${2:-}" = "--all" ]; then rm -rf "$S"; echo "cleaned $WT and $S"; else echo "cleaned $WT (kept $S)"; fi
