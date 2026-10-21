#!/bin/sh
# Stage-2 of a container start (P23-10): runs as root before any service and before the container's
# command (the indus-prepare oneshot under s6, or docker/entrypoint.sh directly when s6 cannot be
# PID 1). It makes the data volume usable by the `indus` user and seeds a first boot:
#
#   1. refuse a `docker run --user` start (R10.25): the steps below need root;
#   2. refuse a data folder (INDUS_HOME) reached through a symlink;
#   3. give `indus` the ids asked for in INDUS_UID / INDUS_GID (or PUID / PGID; default 10000);
#   4. fix ownership inside the data folder only, and only where it is wrong: the folder itself
#      (not its contents) and Indus's own subfolders and files; never through a symlink;
#   5. let `indus` use a bind-mounted Docker socket (the Docker terminal backend);
#   6. keep `.env` owner-only;
#   7. first boot (no config.yaml): seed the settings file and the home as `indus`;
#   8. every start: bring the shipped skills up to date (a new image may ship new ones; the
#      user's edits and deletions are kept, an opt-out is honoured).
#
# `stage2.sh --check-user` runs step 1 only (docker/main-wrapper.sh calls it).
#
# Test seam: _INDUS_IMAGE_ROOT prefixes the fixed image paths (never set in the image).
set -eu

image_root="${_INDUS_IMAGE_ROOT:-}"
install_dir="$image_root/opt/indus"
user=indus
default_id=10000

# Indus's own entries in a home (foundation/layout.py skeleton, profiles, state and settings).
owned_dirs="logs sessions memories skills cron cache backups plugins hooks profiles"
owned_files="config.yaml .env PERSONA.md profile.yaml auth.json state.db state.db-wal state.db-shm"

note() { printf 'indus-prepare: %s\n' "$*"; }
warn() { printf 'indus-prepare: warning: %s\n' "$*" >&2; }
die() {
    printf 'indus-prepare: error: %s\n' "$*" >&2
    exit 1
}

# -- 1. who started us --------------------------------------------------------------------------

check_user() {
    current="$(id -u)"
    if [ "$current" = 0 ] || [ "$current" = "$(id -u "$user")" ]; then
        return 0
    fi
    cat >&2 <<MSG
indus-prepare: error: this container was started as user id $current (docker run --user).
That is not supported: the start needs root to give the data folder your ids, and then every
program runs as the "indus" user anyway.

Start the container as root (the default) and pass your ids instead:

    docker run -e INDUS_UID="\$(id -u)" -e INDUS_GID="\$(id -g)" ...

(PUID and PGID are accepted too.) Files in the data folder are then owned by your user.
MSG
    exit 1
}

if [ "${1:-}" = --check-user ]; then
    check_user
    exit 0
fi
check_user

# -- helpers ------------------------------------------------------------------------------------

owner_uid() { ls -nd "$1" | awk '{print $3}'; }
owner_gid() { ls -nd "$1" | awk '{print $4}'; }

# True when PATH or any folder above it (up to STOP, exclusive; default /) is a symlink.
symlinked() {
    p="$1"
    stop="${2:-/}"
    while [ -n "$p" ] && [ "$p" != / ] && [ "$p" != "$stop" ]; do
        if [ -L "$p" ]; then
            return 0
        fi
        p="$(dirname "$p")"
    done
    return 1
}

valid_id() {
    case "$1" in
        '' | 0* | *[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65534 ]
}

as_indus() {
    if [ "$(id -u)" = 0 ]; then
        s6-setuidgid "$user" "$@"
    else
        "$@"
    fi
}

# -- 2. the data folder -------------------------------------------------------------------------

data="${INDUS_HOME:-/opt/data}"
case "$data" in
    /*) ;;
    *) die "INDUS_HOME must be an absolute path (got '$data')" ;;
esac
while [ "$data" != / ] && [ "${data%/}" != "$data" ]; do
    data="${data%/}"
done
if symlinked "$data"; then
    die "INDUS_HOME ($data) is reached through a symlink; mount the volume at a real path"
fi
INDUS_HOME="$data"
HOME="$data"
export INDUS_HOME HOME
root_run=false
if [ "$(id -u)" = 0 ]; then
    root_run=true
    mkdir -p "$data"
fi

# -- 3. ids -------------------------------------------------------------------------------------

if [ "$root_run" = true ]; then
    want_uid="${INDUS_UID:-${PUID:-$default_id}}"
    want_gid="${INDUS_GID:-${PGID:-$default_id}}"
    valid_id "$want_uid" || die "INDUS_UID/PUID must be a number from 1 to 65534 (got '$want_uid')"
    valid_id "$want_gid" || die "INDUS_GID/PGID must be a number from 1 to 65534 (got '$want_gid')"
    if [ "$want_gid" != "$(id -g "$user")" ]; then
        note "giving group $user the id $want_gid"
        groupmod -o -g "$want_gid" "$user"
    fi
    if [ "$want_uid" != "$(id -u "$user")" ]; then
        note "giving user $user the id $want_uid"
        usermod -o -u "$want_uid" "$user"
    fi
fi
uid="$(id -u "$user")"
gid="$(id -g "$user")"

# -- 4. ownership, inside the data folder only --------------------------------------------------

take() {
    chown "$@" 2>/dev/null || warn "could not change the owner of ${*##* } (rootless runtime?)"
}

if [ "$root_run" = true ]; then
    if [ "$(owner_uid "$data")" != "$uid" ] || [ "$(owner_gid "$data")" != "$gid" ]; then
        note "making $data owned by $user ($uid:$gid); its contents keep their owners"
        take -h "$uid:$gid" "$data"
    fi
    for name in $owned_dirs; do
        path="$data/$name"
        if [ -L "$path" ]; then
            warn "$path is a symlink; its ownership is left alone"
            continue
        fi
        [ -d "$path" ] || continue
        if find "$path" \( ! -uid "$uid" -o ! -gid "$gid" \) -print 2>/dev/null | head -n 1 | grep -q .; then
            note "making $path owned by $user"
            take -R -P "$uid:$gid" "$path"
        fi
    done
    for name in $owned_files; do
        path="$data/$name"
        if [ -L "$path" ]; then
            warn "$path is a symlink; its ownership is left alone"
            continue
        fi
        [ -f "$path" ] || continue
        if [ "$(owner_uid "$path")" != "$uid" ] || [ "$(owner_gid "$path")" != "$gid" ]; then
            take -h "$uid:$gid" "$path"
        fi
    done
fi

# -- 5. a bind-mounted Docker socket ------------------------------------------------------------

if [ "$root_run" = true ]; then
    for sock in "$image_root/var/run/docker.sock" "$image_root/run/docker.sock"; do
        [ -S "$sock" ] || continue
        sock_gid="$(owner_gid "$sock")"
        if id -G "$user" | tr ' ' '\n' | grep -qx "$sock_gid"; then
            break
        fi
        group="$(getent group "$sock_gid" | cut -d: -f1 || true)"
        if [ -z "$group" ]; then
            group=hostdocker
            if ! groupadd -g "$sock_gid" "$group"; then
                warn "could not create a group for the Docker socket; the docker backend may be refused"
                break
            fi
        fi
        if usermod -aG "$group" "$user"; then
            note "$user may use the Docker socket (group $group, $sock_gid)"
        else
            warn "could not add $user to group $group; the docker backend may be refused"
        fi
        break
    done
fi

# -- 6. .env stays owner-only -------------------------------------------------------------------

env_file="$data/.env"
if [ -L "$env_file" ]; then
    warn "$env_file is a symlink; left alone"
else
    if [ ! -e "$env_file" ]; then
        (umask 077 && as_indus touch "$env_file") || warn "could not create $env_file"
    fi
    if [ -f "$env_file" ]; then
        as_indus chmod 600 "$env_file" 2>/dev/null || warn "could not make $env_file owner-only"
    fi
fi

# -- 7. first boot, 8. shipped skills ---------------------------------------------------------

# The log is opened by `indus`, never by root: a symlink planted in the data folder cannot make
# root write somewhere else.
log_dir="$data/logs"
log_file=/dev/null
if ! symlinked "$log_dir/container-start.log" "$data"; then
    if as_indus mkdir -p "$log_dir" && as_indus touch "$log_dir/container-start.log"; then
        log_file="$log_dir/container-start.log"
    fi
fi

logged() {
    as_indus sh -c 'log="$1"; shift; exec "$@" >>"$log" 2>&1' indus-start-log "$log_file" "$@"
}

config_file="$data/config.yaml"
if [ ! -e "$config_file" ] && [ ! -L "$config_file" ]; then
    note "first start: seeding $data (details in logs/container-start.log)"
    # The doctor's exit code reports what is still to do (no provider key yet, ...), not a failure.
    logged indus doctor --fix || true
    chromium_file="$install_dir/.image_chromium"
    if [ -f "$chromium_file" ]; then
        chromium="$(cat "$chromium_file")"
        logged indus config set browser.executable_path "$chromium" ||
            warn "could not point browser.executable_path at $chromium"
    fi
    if [ ! -e "$config_file" ]; then
        warn "no settings file was created; built-in defaults apply (see logs/container-start.log)"
    fi
fi

logged indus skills sync ||
    warn "the shipped skills were not brought up to date (see logs/container-start.log)"

note "ready"
