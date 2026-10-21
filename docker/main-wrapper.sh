#!/bin/sh
# The container's main program (P23-10): decides what the container's arguments mean and runs it
# as the `indus` user.
#
#   (no arguments)            indus
#   -x ... / --flag ...       indus -x ...   a leading dash is always an indus option, never a
#                                            program to look up (`-p work gateway run`)
#   PROGRAM ...               PROGRAM ...    when PROGRAM is an executable on PATH (bash, sleep)
#   anything else             indus ARGS     (`gateway run`, `chat`, `dashboard ...`)
#
# Root drops to `indus` through s6-setuidgid; the `indus` user itself runs directly; any other
# user (a `docker run --user` start) is refused by stage-2's check (R10.25).
#
# Test seam: _INDUS_IMAGE_ROOT prefixes the fixed image paths (never set in the image).
set -eu

image_root="${_INDUS_IMAGE_ROOT:-}"
"$image_root/opt/indus/docker/stage2.sh" --check-user

data="${INDUS_HOME:-/opt/data}"
INDUS_HOME="$data"
HOME="$data"
export INDUS_HOME HOME

start="${INDUS_START_DIR:-}"
if [ -n "$start" ] && [ -d "$start" ]; then
    cd "$start"
elif [ -d "$data" ]; then
    cd "$data"
fi

run_as_indus() {
    if [ "$(id -u)" = 0 ]; then
        exec s6-setuidgid indus "$@"
    fi
    exec "$@"
}

if [ "$#" -eq 0 ]; then
    run_as_indus indus
fi

case "$1" in
    -*) run_as_indus indus "$@" ;;
esac

# Only a program found on PATH counts (a path in the answer), never a shell builtin or function.
found="$(command -v "$1" 2>/dev/null || true)"
case "$found" in
    */*) run_as_indus "$@" ;;
esac

run_as_indus indus "$@"
