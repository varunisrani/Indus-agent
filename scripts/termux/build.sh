#!/usr/bin/env bash
# Termux APT channel build (P24-21): one aarch64 .deb from a release tag, then the signed static
# APT repository that serves it. The stages run in this order:
#
#   1 identity   the tag's Debian version, channel and suite (repo.py; a malformed tag stops here)
#   2 preflight  an aarch64 host, the tools below, and a real base digest in builder.Dockerfile
#   3 source     the tag's tree exported from the checkout (no .git), version stamped, locked
#                requirements exported with uv
#   4 image      the builder image from builder.Dockerfile (its base pinned by digest)
#   5 runtimes   bionic CPython, Node and uv, with their library closure, copied from the image
#                into the payload; package versions recorded in the build manifest
#   6 wheels     the locked requirements built inside the container and retagged
#                android_24_arm64_v8a (ABI cp314)
#   7 venv       an offline install (--network none, --no-index) at the on-device path
#   8 pack       DEBIAN/control and the payload under the Termux prefix, dpkg-deb with xz
#   9 repo       repo.py build: pool, Packages, Release and, with a key, InRelease + Release.gpg
#
# Usage: build.sh --tag <tag> --out <dir> [--source <checkout>] [--key-file <file>] [--dry-run]
#   --source    git checkout holding the tag (default: this repository)
#   --key-file  armoured secret key that signs the Release (passphrase, if any, in
#               TERMUX_APT_SIGNING_PASSPHRASE); without it the build ends with exit 3
#   --dry-run   print the identity and the stage plan, run nothing
#
# Exit codes: 0 built and signed; 2 refused (usage, tag, host, tools, placeholder digest);
# 3 Release unsigned (CI treats 3 as failure); anything else: the named stage failed.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

PLATFORM_TAG="android_24_arm64_v8a"
PYTHON_ABI="cp314"
PACKAGE="indus-agent"
TERMUX_PREFIX="/data/data/com.termux/files/usr"
INSTALL_ROOT="$TERMUX_PREFIX/lib/$PACKAGE"
RUNTIME_PACKAGES=(python nodejs-lts uv)
PLACEHOLDER_DIGEST="sha256:0000000000000000000000000000000000000000000000000000000000000000"
STAGES=(identity preflight source image runtimes wheels venv pack repo)

say() { printf 'termux build: %s\n' "$*" >&2; }
refuse() { say "refused: $*"; exit 2; }
usage() { printf 'usage: build.sh --tag <tag> --out <dir> [--source <dir>] [--key-file <file>] [--dry-run]\n' >&2; exit 2; }
stage() { printf '\n==> stage %s/%s %s: %s\n' "$1" "${#STAGES[@]}" "${STAGES[$(($1 - 1))]}" "$2"; }

base_image() {
    # The one place the base is written down is the Dockerfile's ARG default.
    sed -n 's/^ARG BASE_IMAGE=\([^[:space:]]*\).*/\1/p' "$HERE/builder.Dockerfile" | head -n 1
}

# ---------------------------------------------------------------- container half --------------
# `build.sh --in-container <stage>` runs inside the builder image with the repository's build
# folder mounted at /work. Each stage reads and writes only under /work.

in_container() {
    export PREFIX="$TERMUX_PREFIX"
    export PATH="$PREFIX/bin:${PATH:-/usr/bin:/bin}"
    case "$1" in
        runtimes)
            # Copy every file of the runtime packages and of their installed dependency closure
            # into one prefix-shaped tree; the .deb is self-contained (no Depends line).
            local queue=("${RUNTIME_PACKAGES[@]}") seen=" " pkg dep
            : > /work/payload/manifest.txt
            while [ "${#queue[@]}" -gt 0 ]; do
                pkg="${queue[0]}"; queue=("${queue[@]:1}")
                case "$seen" in *" $pkg "*) continue ;; esac
                seen="$seen$pkg "
                dpkg-query -W "$pkg" >/dev/null 2>&1 || continue  # virtual or not installed
                dpkg-query -W -f='${Package} ${Version}\n' "$pkg" >> /work/payload/manifest.txt
                dpkg -L "$pkg" | while IFS= read -r path; do
                    [ -f "$path" ] || [ -L "$path" ] || continue
                    mkdir -p "/work/payload/runtime$(dirname "$path")"
                    cp -a "$path" "/work/payload/runtime$path"
                done
                for dep in $(dpkg-query -W -f='${Depends}' "$pkg" | tr ',|' '\n\n' | sed 's/(.*)//; s/[[:space:]]//g'); do
                    [ -n "$dep" ] && queue+=("$dep")
                done
            done
            ;;
        wheels)
            # Runs as root (see run_in) so a /bin/sh can exist for configure scripts; the base
            # has none. Native builds are serial (parallel Rust and C builds exhaust arm runners'
            # memory) and maturin needs the API level spelled out off-device.
            [ -e /bin/sh ] || ln -s "$PREFIX/bin/sh" /bin/sh
            export ANDROID_API_LEVEL=24 CARGO_BUILD_JOBS=1 MAKEFLAGS=-j1
            local abi
            abi="$(python3 -c 'import sys; print("cp%d%d" % sys.version_info[:2])')"
            [ "$abi" = "$PYTHON_ABI" ] || { say "image python is $abi, the channel ships $PYTHON_ABI"; exit 1; }
            python3 -m pip wheel --no-cache-dir --wheel-dir /work/wheelhouse \
                --requirement /work/requirements.txt
            local wheel
            for wheel in /work/wheelhouse/*.whl; do
                case "$wheel" in *-none-any.whl) continue ;; esac
                python3 -m wheel tags --remove --platform-tag "$PLATFORM_TAG" "$wheel"
            done
            ;;
        venv)
            python3 -m venv --copies "$INSTALL_ROOT/venv"
            "$INSTALL_ROOT/venv/bin/python" -m pip install --no-index --no-cache-dir \
                --find-links /work/wheelhouse --requirement /work/requirements.txt
            "$INSTALL_ROOT/venv/bin/python" - "$INSTALL_ROOT" <<'PY'
import sys, sysconfig
from pathlib import Path
site = Path(sysconfig.get_paths()["purelib"])
(site / "indus-app.pth").write_text(str(Path(sys.argv[1]) / "app" / "src") + "\n", encoding="utf-8")
PY
            ;;
        *) say "unknown container stage $1"; exit 2 ;;
    esac
}

if [ "${1:-}" = "--in-container" ]; then
    in_container "${2:?stage name}"
    exit 0
fi

# ---------------------------------------------------------------- host half -------------------

TAG=""; OUT=""; SOURCE="$ROOT"; KEY_FILE=""; DRY_RUN=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --tag) TAG="${2:-}"; shift 2 || usage ;;
        --out) OUT="${2:-}"; shift 2 || usage ;;
        --source) SOURCE="${2:-}"; shift 2 || usage ;;
        --key-file) KEY_FILE="${2:-}"; shift 2 || usage ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$TAG" ] && [ -n "$OUT" ] || usage

PY="$(command -v python3 || true)"
[ -n "$PY" ] || refuse "python3 is required"

stage 1 "map $TAG to a Debian version"
DEB_VERSION="$("$PY" "$HERE/repo.py" version "$TAG")" || refuse "$TAG is not a release tag"
CHANNEL="$("$PY" "$HERE/repo.py" channel "$TAG")"
SUITE="indus-$CHANNEL"
BASE="$(base_image)"
printf 'tag %s -> %s %s (suite %s, %s)\n' "$TAG" "$PACKAGE" "$DEB_VERSION" "$SUITE" "$PLATFORM_TAG"
case "$BASE" in
    *"@$PLACEHOLDER_DIGEST") BASE_STATE="placeholder digest (owner-to-fill)" ;;
    *@sha256:*) BASE_STATE="pinned" ;;
    *) BASE_STATE="not pinned" ;;
esac
printf 'builder base %s: %s\n' "$BASE" "$BASE_STATE"

if [ "$DRY_RUN" -eq 1 ]; then
    printf '\nplan (dry run, nothing runs):\n'
    i=1
    for name in "${STAGES[@]}"; do printf '  %s %s\n' "$i" "$name"; i=$((i + 1)); done
    exit 0
fi

stage 2 "check the host, the tools and the pins"
case "$(uname -m)" in aarch64|arm64) ;; *) refuse "build on an aarch64 host (this is $(uname -m))" ;; esac
for tool in docker git uv dpkg-deb; do
    command -v "$tool" >/dev/null 2>&1 || refuse "missing tool: $tool"
done
[ "$BASE_STATE" = "pinned" ] || refuse "builder.Dockerfile base is $BASE_STATE; fill in the digest first"
if [ -n "$KEY_FILE" ] && [ ! -f "$KEY_FILE" ]; then refuse "--key-file $KEY_FILE does not exist"; fi

OUT="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"
WORK="$OUT/work"
rm -rf "$WORK"
mkdir -p "$WORK/app" "$WORK/payload" "$WORK/wheelhouse" "$OUT/debs"

stage 3 "export $TAG from $SOURCE"
git -C "$SOURCE" rev-parse --verify --quiet "refs/tags/$TAG^{commit}" >/dev/null \
    || refuse "tag $TAG is not in $SOURCE"
git -C "$SOURCE" archive --format=tar "$TAG" | tar -x -C "$WORK/app"
[ -f "$WORK/app/pyproject.toml" ] || { say "the tag's tree has no pyproject.toml"; exit 1; }
sed -i "s/^__version__ = \"0\\.0\\.0\"$/__version__ = \"${TAG#v}\"/" "$WORK/app/src/indus/__init__.py"
printf 'apt\n' > "$WORK/app/.install_method"
uv export --frozen --no-dev --no-hashes --no-emit-project \
    --directory "$WORK/app" --output-file "$WORK/requirements.txt" >/dev/null

stage 4 "build the builder image"
DIGEST_HEX="${BASE##*:}"
IMAGE="indus-termux-builder:${DIGEST_HEX:0:12}"
docker build --platform linux/arm64 --file "$HERE/builder.Dockerfile" --tag "$IMAGE" "$HERE"

run_in() {  # run_in <network> <extra docker args...> -- <stage>
    local network="$1"; shift
    local extra=()
    while [ "$1" != "--" ]; do extra+=("$1"); shift; done
    shift
    docker run --rm --platform linux/arm64 --network "$network" \
        -v "$WORK:/work" -v "$HERE/build.sh:/build.sh:ro" "${extra[@]}" \
        "$IMAGE" bash /build.sh --in-container "$1"
}

stage 5 "stage bionic CPython, Node and uv"
mkdir -p "$WORK/payload/runtime" "$WORK/root"
# The image works as uid 1000 (Termux's own user), not the runner's uid: let it write the two
# folders it fills. The work folder is private to this build and deleted at the end.
chmod -R a+rwX "$WORK/payload" "$WORK/root"
run_in none -- runtimes

stage 6 "build and retag the wheels ($PLATFORM_TAG)"
run_in bridge --user 0:0 --tmpfs /bin -- wheels

stage 7 "install the environment offline at $INSTALL_ROOT"
run_in none -v "$WORK/root:$INSTALL_ROOT" -v "$WORK/app:$INSTALL_ROOT/app:ro" -- venv

stage 8 "pack ${PACKAGE}_${DEB_VERSION}_aarch64.deb"
TREE="$WORK/deb"
DEST="$TREE${INSTALL_ROOT}"
mkdir -p "$TREE/DEBIAN" "$DEST" "$TREE$TERMUX_PREFIX/bin"
cp -a "$WORK/root/venv" "$DEST/venv"
cp -a "$WORK/app" "$DEST/app"
cp -a "$WORK/payload/runtime" "$DEST/runtime"
cp "$WORK/payload/manifest.txt" "$DEST/runtime-manifest.txt"
cat > "$TREE$TERMUX_PREFIX/bin/indus" <<EOF
#!$TERMUX_PREFIX/bin/sh
export LD_LIBRARY_PATH="$INSTALL_ROOT/runtime$TERMUX_PREFIX/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$INSTALL_ROOT/venv/bin/python" -m indus "\$@"
EOF
chmod 0755 "$TREE$TERMUX_PREFIX/bin/indus"
cat > "$TREE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $DEB_VERSION
Architecture: aarch64
Maintainer: Indus Agent maintainers <maintainers@indus.invalid>
Installed-Size: $(du -sk "$TREE/data" | cut -f1)
Section: utils
Priority: optional
Description: Indus Agent for Termux
 A self-contained build: its own Python, Node and uv live under
 $INSTALL_ROOT.
EOF
dpkg-deb --build -Zxz --root-owner-group "$TREE" "$OUT/debs/${PACKAGE}_${DEB_VERSION}_aarch64.deb"

stage 9 "assemble the $SUITE repository"
repo_args=(build --debs "$OUT/debs" --out "$OUT/repo" --tag "$TAG")
[ -z "$KEY_FILE" ] || repo_args+=(--key-file "$KEY_FILE")
set +e
"$PY" "$HERE/repo.py" "${repo_args[@]}"
code=$?
set -e
rm -rf "$WORK"
exit "$code"
