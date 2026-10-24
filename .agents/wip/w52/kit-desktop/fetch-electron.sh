#!/bin/bash
# Electron 44.5.1 for the desktop captures, as the repo's e2e lane does it: the official release zip
# through the configured proxy, its SHA-256 checked against the release's SHASUMS256.txt (and the
# electron package's own checksums.json), unpacked into node_modules/electron/dist + path.txt.
#   bash fetch-electron.sh <worktree>          fetch and unpack
#   bash fetch-electron.sh <worktree> --remove delete the unpacked binary again
set -euo pipefail
export S=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/compare/work-i-desktop
export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache UV_CACHE_DIR=/tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/uv-cache-shared npm_config_cache=$S/home/.npm INDUS_HOME=$S/indus
unset TMPDIR
WT=$(cd "${1:?worktree}" && pwd)
E=$(readlink -f "$WT/apps/desktop/node_modules/electron")
[ -f "$E/package.json" ] || { echo "no electron package in $WT"; exit 2; }
if [ "${2:-}" = "--remove" ]; then
  rm -rf "$E/dist" "$E/path.txt"
  echo "removed the Electron binary from $E"
  exit 0
fi
if [ -x "$E/dist/electron" ] && [ "$(cat "$E/path.txt" 2>/dev/null)" = electron ]; then
  echo "Electron already unpacked in $E"
  exit 0
fi
VER=44.5.1
ZIP=electron-v$VER-linux-x64.zip
DL=$S/edl
rm -rf "$DL" && mkdir -p "$DL"
B=https://github.com/electron/electron/releases/download/v$VER
curl -sSfL --cacert /root/.ccr/ca-bundle.crt -o "$DL/SHASUMS256.txt" "$B/SHASUMS256.txt"
curl -sSfL --cacert /root/.ccr/ca-bundle.crt -o "$DL/$ZIP" "$B/$ZIP"
want=$(grep " \*$ZIP\$" "$DL/SHASUMS256.txt" | cut -d' ' -f1)
pkg=$(grep -o "\"$ZIP\": \"[0-9a-f]*\"" "$E/checksums.json" | grep -o '[0-9a-f]\{64\}')
got=$(sha256sum "$DL/$ZIP" | cut -d' ' -f1)
[ -n "$want" ] && [ "$got" = "$want" ] && [ "$got" = "$pkg" ] \
  || { echo "checksum mismatch: got $got want $want pkg $pkg"; rm -rf "$DL"; exit 4; }
echo "sha256 $got matches SHASUMS256.txt and checksums.json"
rm -rf "$E/dist" && mkdir "$E/dist"
unzip -q "$DL/$ZIP" -d "$E/dist"
printf electron > "$E/path.txt"
rm -rf "$DL"
echo "Electron $VER unpacked in $E/dist"
