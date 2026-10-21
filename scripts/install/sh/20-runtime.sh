# shellcheck shell=bash
# Indus Agent installer: prerequisites, runtime, dependencies, config, products, setup, gateway and
# complete stages (P23-05, R10.16).

# The extras every install gets (INS_EXTRAS_COMMON), and those added unless --skip-browser /
# --skip-computer-use (INS_EXTRAS_BROWSER, INS_EXTRAS_COMPUTER_USE) come from stages.json `extras`
# (the generated stages section), the table install.ps1 reads too.
# curl exit codes that mean "the network failed", the only ones that permit the mirror: could not
# resolve proxy/host, could not connect, partial file, HTTP error (-f), timeout, empty reply,
# send/receive failure.
INS_NETWORK_CODES=' 5 6 7 18 22 28 52 55 56 '
INS_LAUNCHER_MARK='# indus launcher: written by install.sh; `indus uninstall` removes it'
INS_PATH_BEGIN='# >>> indus >>>'
INS_PATH_END='# <<< indus <<<'

ins_venv_bin() { printf '%s/.venv/bin/%s' "$IN_DIR" "$1"; }

# -- prerequisites --------------------------------------------------------------------------------

ins_stage_prerequisites() {
    command -v git >/dev/null 2>&1 ||
        ins_fail "git is required; install it with your system's package manager and rerun" git_missing
    if ! git --version >/dev/null 2>&1; then
        if [ "$(uname -s)" = Darwin ]; then
            ins_fail "git needs Apple's Command Line Tools: run 'xcode-select --install', then rerun this installer" git_missing
        fi
        ins_fail "git is installed but does not run: $(git --version 2>&1 | ins_error_line)" git_missing
    fi
    command -v curl >/dev/null 2>&1 ||
        ins_fail "curl is required; install it with your system's package manager and rerun" curl_missing
    command -v tar >/dev/null 2>&1 ||
        ins_fail "tar is required; install it with your system's package manager and rerun" tar_missing
    command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 ||
        ins_fail "sha256sum or shasum is required to verify downloads" sha256_missing
    ins_log "prerequisites: git, curl, tar and a SHA-256 tool found"
}

# -- runtime: pinned uv and Python ----------------------------------------------------------------

# musl or glibc: the ELF interpreter named in the first 8 KiB of a system binary decides, else
# `ldd --version`, else a musl loader on disk.
ins_libc() {
    local probe head out
    if [ $# -eq 0 ]; then set -- /bin/sh; fi
    for probe in "$@"; do
        head="$(head -c 8192 "$probe" 2>/dev/null | LC_ALL=C tr -d '\000')" || continue
        case "$head" in $'\x7f'ELF*) ;; *) continue ;; esac
        case "$head" in
            *ld-musl-*)
                printf musl
                return 0
                ;;
            *ld-linux*)
                printf glibc
                return 0
                ;;
        esac
    done
    out="$(ldd --version 2>&1 | tr '[:upper:]' '[:lower:]')"
    case "$out" in
        *musl*)
            printf musl
            return 0
            ;;
        *glibc* | *"gnu libc"* | *"gnu c library"*)
            printf glibc
            return 0
            ;;
    esac
    if ls /lib/ld-musl-*.so.1 >/dev/null 2>&1; then printf musl; else printf glibc; fi
}

# The pins key for this host: linux-x64, linux-arm64-musl, darwin-arm64, ...
ins_uv_target() {
    local arch
    case "$(uname -m)" in
        x86_64 | amd64) arch=x64 ;;
        arm64 | aarch64) arch=arm64 ;;
        *) return 1 ;;
    esac
    case "$(uname -s)" in
        Linux)
            if [ "$(ins_libc)" = musl ]; then printf 'linux-%s-musl' "$arch"; else printf 'linux-%s' "$arch"; fi
            ;;
        Darwin) printf 'darwin-%s' "$arch" ;;
        *) return 1 ;;
    esac
}

ins_sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        return 1
    fi
}

# ins_download URL FILE: returns curl's exit status. HTTPS only, redirects included (the digest is
# checked afterwards whatever the origin).
ins_download() {
    ins_log "downloading $1"
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 600 \
        -o "$2" "$1"
}

ins_is_network_failure() {
    case "$INS_NETWORK_CODES" in *" $1 "*) return 0 ;; esac
    return 1
}

# The pinned uv under <home>/tools/uv-<version>-<target>/, downloaded and verified if missing.
ins_ensure_uv() {
    [ -z "${INS_UV:-}" ] || return 0
    local target entry tmp rc origin digest found
    target="$(ins_uv_target)" ||
        ins_fail "there is no uv build for $(uname -s) $(uname -m); this system is not supported" unsupported_platform
    ins_uv_pin "$target" ||
        ins_fail "no uv is pinned for $target; this system is not supported" unsupported_platform
    entry="$IN_HOME/tools/uv-$INS_UV_VERSION-$target"
    if [ ! -x "$entry/uv" ]; then
        [ -n "$INS_PIN_SHA256" ] ||
            ins_fail "this installer has no recorded SHA-256 for uv $INS_UV_VERSION ($target) yet, so it downloads nothing; use a released install.sh" download_failed
        tmp="$(mktemp -d 2>/dev/null)" || ins_fail "cannot create a temporary directory" filesystem_error
        INS_CLEANUP="$tmp"
        origin="$INS_PIN_URL"
        ins_download "$INS_PIN_URL" "$tmp/uv.archive"
        rc=$?
        if [ "$rc" -ne 0 ]; then
            ins_is_network_failure "$rc" ||
                ins_fail "downloading uv from $INS_PIN_URL failed (curl exit $rc)" download_failed
            [ -n "$INS_PIN_MIRROR" ] ||
                ins_fail "downloading uv from $INS_PIN_URL failed (curl exit $rc) and there is no mirror" download_failed
            ins_warn "downloading uv failed (curl exit $rc); trying our mirror"
            origin="$INS_PIN_MIRROR"
            rm -f "$tmp/uv.archive"
            ins_download "$INS_PIN_MIRROR" "$tmp/uv.archive"
            rc=$?
            [ "$rc" -eq 0 ] ||
                ins_fail "downloading uv failed from $INS_PIN_URL and from the mirror (curl exit $rc)" download_failed
        fi
        digest="$(ins_sha256_file "$tmp/uv.archive")" ||
            ins_fail "cannot compute the SHA-256 of the uv download" download_failed
        [ "$digest" = "$INS_PIN_SHA256" ] ||
            ins_fail "the uv download from $origin does not match its pinned SHA-256 (expected $INS_PIN_SHA256, got $digest)" download_digest_mismatch
        mkdir -p "$tmp/unpacked" && tar -xzf "$tmp/uv.archive" -C "$tmp/unpacked" 2>/dev/null ||
            ins_fail "cannot unpack the uv archive" uv_unusable
        found="$(find "$tmp/unpacked" -type f -name uv | head -n 1)"
        [ -n "$found" ] || ins_fail "the uv archive holds no uv binary" uv_unusable
        mkdir -p "$entry" || ins_fail "cannot create $entry" filesystem_error
        if [ -f "${found%/uv}/uvx" ]; then mv -f "${found%/uv}/uvx" "$entry/uvx" && chmod 755 "$entry/uvx"; fi
        mv -f "$found" "$entry/uv" && chmod 755 "$entry/uv" ||
            ins_fail "cannot install uv into $entry" filesystem_error
        rm -rf "$tmp"
        INS_CLEANUP=""
    fi
    if ! "$entry/uv" --version >/dev/null 2>&1; then
        rm -rf "$entry"
        ins_fail "the pinned uv does not run on this system" uv_unusable
    fi
    INS_UV="$entry/uv"
    export UV_PYTHON_INSTALL_DIR="$IN_HOME/tools/python"
}

# An interpreter inside the supported range (an existing 3.13/3.14 is fine), else uv installs the
# pinned minor under <home>/tools/python.
ins_find_python() {
    [ -z "${INS_PYTHON:-}" ] || return 0
    local py
    ins_ensure_uv
    py="$("$INS_UV" python find --no-project "$INS_PYTHON_RANGE" 2>/dev/null)" || py=""
    if [ -z "$py" ]; then
        ins_run "Installing Python $INS_PYTHON_VERSION" "$INS_UV" python install --no-bin "$INS_PYTHON_VERSION" ||
            ins_fail "Python $INS_PYTHON_VERSION could not be installed$(ins_cause)" python_install_failed
        py="$("$INS_UV" python find --no-project "$INS_PYTHON_VERSION" 2>/dev/null)" ||
            ins_fail "Python $INS_PYTHON_VERSION was installed but cannot be found" python_install_failed
    fi
    py="${py%$'\r'}"
    [ -x "$py" ] && "$py" --version >/dev/null 2>&1 ||
        ins_fail "the Python interpreter $py does not run" python_install_failed
    INS_PYTHON="$py"
}

ins_stage_runtime() {
    ins_find_python
    ins_ok "uv $INS_UV_VERSION and $("$INS_PYTHON" --version 2>&1) ready"
}

# -- dependencies ---------------------------------------------------------------------------------

# ins_left_out FLAG EXTRAS: true when these extras stay out of this install: this run's skip flag
# says so, or an earlier install saved its extras (updates.installs.<id>.extras) without them. A
# group with no extras is left out only by its flag.
ins_left_out() {
    local saved word
    [ "$1" = false ] || return 0
    [ -n "$2" ] || return 1
    saved="$(ins_install_setting "$IN_DIR" extras 2>/dev/null)"
    [ -n "$saved" ] || return 1
    for word in $2; do
        case " $saved " in *" $word "*) ;; *) return 0 ;; esac
    done
    return 1
}

# The extras this install syncs, in a fixed order: the common ones, then the optional groups that
# are not left out.
ins_extras_words() {
    local words="$INS_EXTRAS_COMMON"
    ins_left_out "$IN_SKIP_BROWSER" "$INS_EXTRAS_BROWSER" || words="$words $INS_EXTRAS_BROWSER"
    ins_left_out "$IN_SKIP_CU" "$INS_EXTRAS_COMPUTER_USE" || words="$words $INS_EXTRAS_COMPUTER_USE"
    printf '%s' "$words"
}

# What is left out, for the person: tool names, not extras.
ins_left_out_words() {
    local out=""
    if ins_left_out "$IN_SKIP_BROWSER" "$INS_EXTRAS_BROWSER"; then out="browser"; fi
    if ins_left_out "$IN_SKIP_CU" "$INS_EXTRAS_COMPUTER_USE"; then out="${out:+$out }computer-use"; fi
    printf '%s' "$out"
}

ins_stage_dependencies() {
    local extras=() word left
    [ -f "$IN_DIR/uv.lock" ] || ins_fail "$IN_DIR has no uv.lock; run the repository stage first" deps_install_failed
    ins_find_python
    for word in $(ins_extras_words); do extras+=(--extra "$word"); done
    left="$(ins_left_out_words)"
    unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV
    ins_run "Installing the Python packages (hash-checked against uv.lock)" \
        "$INS_UV" sync --directory "$IN_DIR" --frozen --no-dev --python "$INS_PYTHON" "${extras[@]}" ||
        ins_fail "installing the Python packages failed$(ins_cause)" deps_install_failed
    [ -x "$(ins_venv_bin indus)" ] || ins_fail "the indus command is missing after the install" deps_install_failed
    if [ -n "$left" ]; then ins_info "left out: $left"; fi
}

# -- config ---------------------------------------------------------------------------------------

# The installed package's own code makes the home skeleton and seeds config.yaml (the same writer
# `indus config edit` uses), so the installer never duplicates the layout.
INS_SEED_PY='import sys
from pathlib import Path

from indus.cli.commands.config_read import seed_config
from indus.foundation.layout import ensure_home
from indus.foundation.scope import make_scope

scope = make_scope(Path(sys.argv[1]))
ensure_home(scope)
seed_config(scope)'

ins_seed_home() {
    local py
    py="$(ins_venv_bin python)"
    [ -x "$py" ] || ins_fail "the Python environment is missing; run the dependencies stage first" deps_missing
    ins_run "Preparing $IN_HOME" "$py" -I -c "$INS_SEED_PY" "$IN_HOME" ||
        ins_fail "could not prepare $IN_HOME$(ins_cause)" config_failed
}

# ins_set_install_setting NAME VALUE: updates.installs.<id>.<NAME> through `indus config set`
# (the schema key `indus update` reads: channel, extras).
ins_set_install_setting() {
    local id
    id="$(ins_install_id "$IN_DIR")" || return 1
    ins_run --may-fail "Saving updates.installs.$id.$1" \
        "$(ins_venv_bin indus)" config set --force "updates.installs.$id.$1" "$2"
}

ins_stage_config() {
    local words list="" word
    ins_seed_home
    if [ ! -e "$IN_HOME/.env" ]; then
        (umask 077 && : >"$IN_HOME/.env") || ins_fail "cannot create $IN_HOME/.env" config_failed
    fi
    chmod 600 "$IN_HOME/.env" || ins_fail "cannot restrict $IN_HOME/.env to its owner" config_failed
    # The extras this install synced, so `indus update` and later installs keep the same set.
    words="$(ins_extras_words)"
    for word in $words; do list="${list:+$list, }$word"; done
    ins_set_install_setting extras "[$list]" ||
        ins_warn "could not save the extras ($words); later updates may install a different set"
    ins_log "settings ready in $IN_HOME"
}

# -- products -------------------------------------------------------------------------------------

# Paths written into the launcher must survive double quotes in /bin/sh.
ins_plain_path() {
    case "$1" in *'"'* | *'$'* | *'`'* | *'\'* | *$'\n'*) return 1 ;; esac
    return 0
}

ins_publish_launcher() {
    local program="$1" bin="$HOME/.local/bin" launcher tmp
    launcher="$bin/indus"
    ins_plain_path "$program" && ins_plain_path "$IN_HOME" ||
        ins_fail "the install path holds quotes, \$, \` or backslashes; choose another --dir / --indus-home" filesystem_error
    if [ -e "$launcher" ] || [ -L "$launcher" ]; then
        if [ -L "$launcher" ] || ! grep -qF "$INS_LAUNCHER_MARK" "$launcher" 2>/dev/null; then
            ins_warn "$launcher was not written by this installer; leaving it (this install's command: $program)"
            return 0
        fi
    fi
    mkdir -p "$bin" || ins_fail "cannot create $bin" filesystem_error
    tmp="$bin/.indus.$$.tmp"
    {
        printf '#!/bin/sh\n%s\n' "$INS_LAUNCHER_MARK"
        if [ "$IN_HOME" != "$HOME/.indus" ]; then
            # shellcheck disable=SC2016 # the launcher expands it, not this script
            printf 'INDUS_HOME="${INDUS_HOME:-%s}"\nexport INDUS_HOME\n' "$IN_HOME"
        fi
        # shellcheck disable=SC2016
        printf 'exec "%s" "$@"\n' "$program"
    } >"$tmp" && chmod 755 "$tmp" && mv -f "$tmp" "$launcher" ||
        { rm -f "$tmp"; ins_fail "cannot write $launcher" filesystem_error; }
    ins_log "published $launcher"
}

# ins_add_path_block RC BODY: append the marked block once (an existing block is left alone;
# `indus uninstall` removes it by its exact marker lines).
ins_add_path_block() {
    local rc="$1"
    if [ -f "$rc" ] && grep -qxF "$INS_PATH_BEGIN" "$rc"; then return 0; fi
    mkdir -p "$(dirname "$rc")" || ins_fail "cannot create $(dirname "$rc")" filesystem_error
    {
        if [ -s "$rc" ]; then printf '\n'; fi
        printf '%s\n%s\n%s\n' "$INS_PATH_BEGIN" "$2" "$INS_PATH_END"
    } >>"$rc" || ins_fail "cannot update $rc" filesystem_error
    ins_log "added ~/.local/bin to PATH in $rc"
}

ins_wire_path() {
    local posix_line rc zdir
    # shellcheck disable=SC2016 # written to the rc file literally, expanded by the shell that reads it
    posix_line='case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh)
            zdir="${ZDOTDIR:-$HOME}"
            ins_add_path_block "$zdir/.zshrc" "$posix_line"
            ins_add_path_block "$zdir/.zprofile" "$posix_line"
            ;;
        fish)
            # shellcheck disable=SC2016
            ins_add_path_block "$HOME/.config/fish/config.fish" 'fish_add_path "$HOME/.local/bin"'
            ;;
        *)
            ins_add_path_block "$HOME/.bashrc" "$posix_line"
            ins_add_path_block "$HOME/.profile" "$posix_line"
            # A login bash reads only the first of these it finds, before .profile.
            for rc in "$HOME/.bash_profile" "$HOME/.bash_login"; do
                if [ -f "$rc" ]; then ins_add_path_block "$rc" "$posix_line"; fi
            done
            ;;
    esac
}

ins_stage_products() {
    local program
    program="$(ins_venv_bin indus)"
    [ -x "$program" ] || ins_fail "$program is missing; run the dependencies stage first" deps_missing
    ins_publish_launcher "$program"
    # A source checkout serves its own built pages (apps/web/dist, apps/tui/dist); building them
    # from a release payload waits for the release artefact manifest.
    if [ ! -f "$IN_DIR/apps/web/dist/index.html" ]; then
        ins_log "the dashboard pages are not built in this checkout; 'indus dashboard' will say so"
    fi
    if [ "$IN_DESKTOP" = true ]; then
        ins_warn "install.sh does not build the desktop app; download it from the release page (it uses this runtime)"
    fi
    ins_wire_path
}

ins_path_hint() {
    case ":$PATH:" in *":$HOME/.local/bin:"*) return 0 ;; esac
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh) ins_info "Open a new terminal (or run: source ~/.zshrc) to use the indus command" ;;
        fish) ins_info "Open a new terminal (or run: source ~/.config/fish/config.fish) to use the indus command" ;;
        *) ins_info "Open a new terminal (or run: source ~/.bashrc) to use the indus command" ;;
    esac
}

# -- setup and gateway (a person at a terminal only) ----------------------------------------------

# Under `curl | bash` stdin is the script, so the questions go to the terminal itself; opening it
# fails without one (a container build, the desktop's runner).
ins_has_tty() { (: </dev/tty) 2>/dev/null; }

ins_stage_setup() {
    ins_has_tty || ins_skip "no terminal; run 'indus setup' later"
    "$(ins_venv_bin indus)" setup </dev/tty ||
        ins_fail "provider setup did not finish; run 'indus setup' to try again" setup_failed
}

ins_stage_gateway() {
    local answer=""
    ins_has_tty || ins_skip "no terminal; run 'indus gateway install' later"
    printf 'Run the messaging gateway as a background service now? [y/N] '
    IFS= read -r answer </dev/tty || answer=""
    case "$answer" in
        y | Y | yes | YES) ;;
        *) ins_skip "not requested; run 'indus gateway install' any time" ;;
    esac
    "$(ins_venv_bin indus)" gateway install </dev/tty ||
        ins_fail "the gateway service was not installed; run 'indus gateway install' to try again" gateway_failed
}

# -- complete -------------------------------------------------------------------------------------

ins_stage_complete() {
    local commit url saved="" channel marker tmp
    commit="$(git -C "$IN_DIR" rev-parse HEAD 2>/dev/null)" ||
        ins_fail "$IN_DIR is not a checkout; run the repository stage first" other
    url="$(git -C "$IN_DIR" config --get remote.origin.url 2>/dev/null)" || url=""
    [ -n "$IN_CHANNEL$IN_COMMIT" ] || saved="$(ins_saved_channel "$IN_DIR")"
    channel="$(ins_effective_channel "$url" "$saved")"
    marker="$IN_DIR/.indus-bootstrap.json"
    tmp="$marker.$$.tmp"
    printf '{"schema":1,"commit":%s,"branch":%s,"channel":%s,"completed_at":"%s"}\n' \
        "$(ins_json_string "$commit")" "$(ins_json_string "$IN_BRANCH")" \
        "$(ins_json_string "$channel")" "$(ins_now)" >"$tmp" && mv -f "$tmp" "$marker" ||
        { rm -f "$tmp"; ins_fail "cannot write $marker" filesystem_error; }
    if [ -n "$IN_CHANNEL" ]; then
        ins_set_install_setting channel "$IN_CHANNEL" ||
            ins_warn "could not save the $IN_CHANNEL channel; 'indus update --set-channel $IN_CHANNEL' does it"
    fi
    ins_ok "Indus Agent is installed. Run: indus"
}
