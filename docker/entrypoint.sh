#!/bin/sh
# Container entry point (P23-10).
#
# As PID 1 it hands the container to s6-overlay's /init with docker/main-wrapper.sh as the main
# program: /init runs the indus-prepare oneshot (docker/stage2.sh) and the enabled services, then
# the wrapper with the container's arguments; the container ends when the wrapper's program ends.
#
# When something else is PID 1 (`docker run --init`, a platform's own init), /init cannot run, so
# this script runs stage-2 itself and then the wrapper: the command still works, the supervised
# gateway/dashboard services do not.
#
# Test seams (never set in the image): _INDUS_IMAGE_ROOT prefixes the fixed image paths and
# _INDUS_ENTRY_PID stands in for this shell's pid.
set -eu

image_root="${_INDUS_IMAGE_ROOT:-}"
docker_dir="$image_root/opt/indus/docker"

# The wrapper returns here before running the command (the `docker run -w` directory).
INDUS_START_DIR="${INDUS_START_DIR:-$PWD}"
export INDUS_START_DIR

if [ "${_INDUS_ENTRY_PID:-$$}" = 1 ]; then
    # An interactive command (`docker run -it IMAGE chat`) owns the terminal: send the services'
    # output to s6's own log instead of across the screen, unless the user chose otherwise.
    if [ -t 0 ] && [ -t 1 ]; then
        S6_LOGGING="${S6_LOGGING:-2}"
        S6_CMD_USE_TERMINAL="${S6_CMD_USE_TERMINAL:-1}"
        export S6_LOGGING S6_CMD_USE_TERMINAL
    fi
    exec "$image_root/init" "$docker_dir/main-wrapper.sh" "$@"
fi

echo "indus: the container's init is not this image's (PID 1 is another program);" \
    "starting without the supervisor: the gateway and dashboard services are off," \
    "the command still runs" >&2
PATH="$image_root/command:$PATH"
export PATH
"$docker_dir/stage2.sh"
exec "$docker_dir/main-wrapper.sh" "$@"
