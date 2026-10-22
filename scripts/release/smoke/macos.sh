#!/usr/bin/env bash
# Post-release smoke test for macOS (P25-23).
#
#   bash macos.sh --tag vX.Y.Z [--repository OWNER/NAME] [--channel stable|canary]
#                 [--workdir DIR] [--desktop auto|skip|FILE] [--interactive] [--keep] [--dry-run]
#
# Run it on a clean machine or VM right after a release, and on the canary rehearsal artefacts.
# It uses only what the release published: install.sh from the release's downloads, the desktop
# DMG from the same release, and the release's own checkout that install.sh puts on disk
# (its scripted model server is the fake-model endpoint, so no real model or key is needed).
# Everything runs inside one throwaway folder with its own HOME, so the person's own setup, keys
# and PATH are never touched.
#
# Each step prints one line, `<step> PASS|FAIL|SKIP <detail>`, and the run ends with a summary.
# Exit 0 when no step failed, 1 otherwise, 2 for a usage error. --dry-run prints the plan and the
# URLs and fetches nothing. The steps and what they prove are listed in docs/internals/rollback.md
# (smoke section) and the results go into docs/internals/rehearsal-log.md.

set -u

SMOKE_OS=macos
# The release base (RELEASE_REPOSITORY in src/indus/foundation/versions.py, D-16.690); a unit test
# holds this default equal to it.
SMOKE_DEFAULT_REPOSITORY='indusagi/indus-agent'
SMOKE_STEPS=(install version setup turn approval resume doctor dashboard-gate dashboard-login
    desktop-download licences signature desktop-launch uninstall)
SMOKE_INSTALL_WAIT=1800
SMOKE_TURN_WAIT=180
SMOKE_DASHBOARD_WAIT=600
SMOKE_DESKTOP_WAIT=20

smoke_usage() {
    cat <<'EOF'
Usage: macos.sh --tag vX.Y.Z [options]

  --tag TAG                 the release to check (vX.Y.Z or vX.Y.Z-canary.<stamp>)
  --repository OWNER/NAME   where the release lives (default: the release base)
  --channel stable|canary   the installer channel (default: from the tag)
  --workdir DIR             the throwaway folder (default: a new temp folder)
  --desktop auto|skip|FILE  the DMG: auto (download it), skip, or a local file
  --interactive             ask a person to confirm what a script cannot see
  --keep                    keep the work folder even when every step passed
  --dry-run                 print the plan and the URLs; fetch nothing
  -h, --help                this help
EOF
}

smoke_describe() {
    case "$1" in
        install) printf 'download install.sh from the release and install the %s channel' "$SMOKE_CHANNEL" ;;
        version) printf 'indus --version; indus update --check reports this release' ;;
        setup) printf 'indus setup --non-interactive; point the model at the fake endpoint' ;;
        turn) printf 'one turn through the gateway daemon (indus -z)' ;;
        approval) printf 'a risky command without a person to approve it does not run' ;;
        resume) printf 'resume the first session by id (indus -r ID -z)' ;;
        doctor) printf 'indus doctor passes' ;;
        dashboard-gate) printf 'dashboard on loopback: status public, settings refuse a request without the token' ;;
        dashboard-login) printf 'dashboard page carries the loopback session token and the API accepts it' ;;
        desktop-download) printf 'download the DMG from the release and mount it read-only' ;;
        signature) printf 'the app is signed and notarised (codesign, spctl)' ;;
        licences) printf 'THIRD_PARTY_NOTICES.md in the checkout and inside the desktop package' ;;
        desktop-launch) printf 'start the desktop app and reach the chat (a person confirms)' ;;
        uninstall) printf 'indus uninstall -y removes code and launcher and keeps the data' ;;
        *) printf '?' ;;
    esac
}

# -- arguments ------------------------------------------------------------------------------------

smoke_parse() {
    SMOKE_TAG=""
    SMOKE_REPOSITORY="$SMOKE_DEFAULT_REPOSITORY"
    SMOKE_CHANNEL=""
    SMOKE_WORK=""
    SMOKE_DESKTOP=auto
    SMOKE_INTERACTIVE=false
    SMOKE_KEEP=false
    SMOKE_DRY_RUN=false
    while [ $# -gt 0 ]; do
        case "$1" in
            --tag | --repository | --channel | --workdir | --desktop)
                [ $# -ge 2 ] || { printf '%s needs a value\n' "$1" >&2; exit 2; }
                case "$1" in
                    --tag) SMOKE_TAG="$2" ;;
                    --repository) SMOKE_REPOSITORY="$2" ;;
                    --channel) SMOKE_CHANNEL="$2" ;;
                    --workdir) SMOKE_WORK="$2" ;;
                    --desktop) SMOKE_DESKTOP="$2" ;;
                esac
                shift 2
                ;;
            --interactive) SMOKE_INTERACTIVE=true; shift ;;
            --keep) SMOKE_KEEP=true; shift ;;
            --dry-run) SMOKE_DRY_RUN=true; shift ;;
            -h | --help) smoke_usage; exit 0 ;;
            *) printf 'unknown option: %s\n' "$1" >&2; smoke_usage >&2; exit 2 ;;
        esac
    done
    if ! printf '%s\n' "$SMOKE_TAG" | grep -Eq '^v[0-9]{1,3}\.[0-9]+\.[0-9]+(-canary\.[0-9]{8}T[0-9]{6}Z)?$'; then
        printf 'a release tag is required: --tag vX.Y.Z or vX.Y.Z-canary.<YYYYMMDDTHHMMSSZ>\n' >&2
        exit 2
    fi
    if ! printf '%s\n' "$SMOKE_REPOSITORY" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then
        printf -- '--repository must be OWNER/NAME\n' >&2
        exit 2
    fi
    SMOKE_VERSION="${SMOKE_TAG#v}"
    if [ -z "$SMOKE_CHANNEL" ]; then
        case "$SMOKE_VERSION" in *-canary.*) SMOKE_CHANNEL=canary ;; *) SMOKE_CHANNEL=stable ;; esac
    fi
    case "$SMOKE_CHANNEL" in stable | canary) ;; *) printf -- '--channel must be stable or canary\n' >&2; exit 2 ;; esac
    SMOKE_RELEASE_URL="https://github.com/$SMOKE_REPOSITORY/releases/download/$SMOKE_TAG"
}

# The DMG names electron-builder writes for this machine (artifactName
# Indus-<version>-mac-<arch>.dmg).
smoke_dmg_names() {
    case "$(uname -m)" in
        arm64) printf 'Indus-%s-mac-arm64.dmg\n' "$SMOKE_VERSION" ;;
        x86_64) printf 'Indus-%s-mac-x64.dmg\n' "$SMOKE_VERSION" ;;
        *) return 1 ;;
    esac
}

smoke_plan() {
    local step number=0 name
    printf 'Indus Agent post-release smoke (%s)\n' "$SMOKE_OS"
    printf '  release:    %s (%s channel) of %s\n' "$SMOKE_TAG" "$SMOKE_CHANNEL" "$SMOKE_REPOSITORY"
    printf '  installer:  %s/install.sh\n' "$SMOKE_RELEASE_URL"
    if [ "$SMOKE_DESKTOP" = auto ]; then
        while IFS= read -r name; do printf '  desktop:    %s/%s\n' "$SMOKE_RELEASE_URL" "$name"; done < <(smoke_dmg_names)
    else
        printf '  desktop:    %s\n' "$SMOKE_DESKTOP"
    fi
    printf '  fake model: scripts/parity/scripted_model_server.py from the installed checkout\n'
    for step in "${SMOKE_STEPS[@]}"; do
        number=$((number + 1))
        printf '  %2d. %-17s %s\n' "$number" "$step" "$(smoke_describe "$step")"
    done
}

# -- results and children -------------------------------------------------------------------------

SMOKE_FAILED=0
SMOKE_RESULTS=""

smoke_result() { # STEP STATUS DETAIL
    printf '%-17s %s %s\n' "$1" "$2" "$3"
    SMOKE_RESULTS="${SMOKE_RESULTS}$(printf '%-17s %s %s' "$1" "$2" "$3")
"
    if [ "$2" = FAIL ]; then SMOKE_FAILED=$((SMOKE_FAILED + 1)); fi
}

# bounded SECONDS CMD...: run CMD (stdin from /dev/null) and stop it, with what it started, by
# its own pid after SECONDS. Returns CMD's status, or 124 when it was stopped.
bounded() {
    local limit=$(($1 * 5)) pid ticks=0
    shift
    "$@" </dev/null &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$ticks" -ge "$limit" ]; then
            stop_tree "$pid"
            wait "$pid" 2>/dev/null
            return 124
        fi
        sleep 0.2
        ticks=$((ticks + 1))
    done
    wait "$pid"
}

# stop_tree PID: TERM a child we started and everything it started (by parent pid, never by name).
stop_tree() {
    local pid="$1" child
    [ -n "$pid" ] || return 0
    for child in $(pgrep -P "$pid" 2>/dev/null); do stop_tree "$child"; done
    kill -TERM "$pid" 2>/dev/null || :
}

# stop_child PID: stop_tree, then reap the child itself.
stop_child() {
    [ -n "$1" ] || return 0
    stop_tree "$1"
    wait "$1" 2>/dev/null || :
}

last_line() { grep -v '^[[:space:]]*$' "$1" 2>/dev/null | tail -n 1 | cut -c1-160; }

# -- the steps ------------------------------------------------------------------------------------

smoke_isolate() {
    local name
    for name in $(compgen -e); do
        case "$name" in INDUS_*) unset "$name" ;; esac
    done
    export HOME="$SMOKE_WORK/home"
    export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share"
    export XDG_CACHE_HOME="$HOME/.cache" XDG_STATE_HOME="$HOME/.local/state"
    export INDUS_TERMINAL_ID=smoke NO_COLOR=1 LANG=C.UTF-8 TZ=UTC
    unset VIRTUAL_ENV UV_PROJECT_ENVIRONMENT
    mkdir -p "$HOME" "$SMOKE_LOGS" "$SMOKE_WORK/project" || exit 1
    SMOKE_INDUS="$HOME/.local/bin/indus"
    SMOKE_CHECKOUT="$HOME/.indus/indus-agent"
    SMOKE_DATA="$HOME/.indus"
}

step_install() {
    local installed
    if ! bounded 300 curl -fsSL --proto '=https' --tlsv1.2 -o "$SMOKE_WORK/install.sh" \
        "$SMOKE_RELEASE_URL/install.sh" 2>"$SMOKE_LOGS/install-download.log"; then
        smoke_result install FAIL "could not download $SMOKE_RELEASE_URL/install.sh (is the installer attached to the release?)"
        return 1
    fi
    if ! bounded "$SMOKE_INSTALL_WAIT" bash "$SMOKE_WORK/install.sh" --channel "$SMOKE_CHANNEL" \
        --non-interactive --skip-browser >"$SMOKE_LOGS/install.log" 2>&1; then
        smoke_result install FAIL "install.sh failed: $(last_line "$SMOKE_LOGS/install.log")"
        return 1
    fi
    if [ ! -x "$SMOKE_INDUS" ]; then
        smoke_result install FAIL "no launcher at $SMOKE_INDUS after the install"
        return 1
    fi
    installed="$(git -C "$SMOKE_CHECKOUT" describe --tags --exact-match HEAD 2>/dev/null)"
    if [ "$installed" != "$SMOKE_TAG" ]; then
        smoke_result install FAIL "installed ${installed:-an untagged commit}, expected $SMOKE_TAG (a newer $SMOKE_CHANNEL release?)"
        return 1
    fi
    smoke_result install PASS "$SMOKE_TAG in $SMOKE_CHECKOUT"
}

step_version() {
    local out
    if ! bounded 60 "$SMOKE_INDUS" --version >"$SMOKE_LOGS/version.log" 2>&1 ||
        ! grep -q '^Indus Agent ' "$SMOKE_LOGS/version.log" ||
        ! grep -q '^Install method: git' "$SMOKE_LOGS/version.log"; then
        smoke_result version FAIL "indus --version: $(last_line "$SMOKE_LOGS/version.log")"
        return 1
    fi
    bounded 120 "$SMOKE_INDUS" update --check >"$SMOKE_LOGS/update-check.log" 2>&1
    out="$(last_line "$SMOKE_LOGS/update-check.log")"
    case "$out" in
        *"already on $SMOKE_VERSION"*) smoke_result version PASS "$(head -n 1 "$SMOKE_LOGS/version.log"); $out" ;;
        *) smoke_result version FAIL "indus update --check: $out"; return 1 ;;
    esac
}

SMOKE_MODEL_PID=""

step_setup() {
    local python server port="" tries=0 key value
    if ! bounded 120 "$SMOKE_INDUS" setup --non-interactive >"$SMOKE_LOGS/setup.log" 2>&1; then
        smoke_result setup FAIL "indus setup --non-interactive: $(last_line "$SMOKE_LOGS/setup.log")"
        return 1
    fi
    python="$SMOKE_CHECKOUT/.venv/bin/python"
    server="$SMOKE_CHECKOUT/scripts/parity/scripted_model_server.py"
    if [ ! -f "$server" ]; then
        smoke_result setup FAIL "the release checkout has no $server"
        return 1
    fi
    # The server serves until its stdin closes: a FIFO held open by this shell keeps it up.
    mkfifo "$SMOKE_WORK/model.in" && exec 7<>"$SMOKE_WORK/model.in"
    "$python" "$server" --port 0 --default-script echo <"$SMOKE_WORK/model.in" \
        >"$SMOKE_WORK/model.out" 2>"$SMOKE_LOGS/model.log" &
    SMOKE_MODEL_PID=$!
    while [ -z "$port" ] && [ "$tries" -lt 50 ]; do
        port="$(sed -n 's/.*"port": *\([0-9][0-9]*\).*/\1/p' "$SMOKE_WORK/model.out" 2>/dev/null)"
        [ -n "$port" ] || { sleep 0.2; tries=$((tries + 1)); }
    done
    if [ -z "$port" ]; then
        smoke_result setup FAIL "the fake model did not start: $(last_line "$SMOKE_LOGS/model.log")"
        return 1
    fi
    for key in model.provider model.base_url model.default; do
        case "$key" in
            model.provider) value=custom ;;
            model.base_url) value="http://127.0.0.1:$port/v1" ;;
            model.default) value=echo ;;
        esac
        if ! bounded 60 "$SMOKE_INDUS" config set "$key" "$value" >>"$SMOKE_LOGS/setup.log" 2>&1; then
            smoke_result setup FAIL "indus config set $key: $(last_line "$SMOKE_LOGS/setup.log")"
            return 1
        fi
    done
    # Secrets stay in the throwaway home, never in this machine's keychain.
    if ! bounded 60 "$SMOKE_INDUS" config set security.secret_store file >>"$SMOKE_LOGS/setup.log" 2>&1; then
        smoke_result setup FAIL "indus config set security.secret_store: $(last_line "$SMOKE_LOGS/setup.log")"
        return 1
    fi
    smoke_result setup PASS "fake model on 127.0.0.1:$port"
}

step_turn() {
    (cd "$SMOKE_WORK/project" && bounded "$SMOKE_TURN_WAIT" "$SMOKE_INDUS" -z "remember the blue kite") \
        >"$SMOKE_LOGS/turn.log" 2>&1
    if grep -q 'You said: remember the blue kite' "$SMOKE_LOGS/turn.log"; then
        smoke_result turn PASS "the reply came back through the gateway daemon"
    else
        smoke_result turn FAIL "$(last_line "$SMOKE_LOGS/turn.log")"
        return 1
    fi
}

step_approval() {
    local project="$SMOKE_WORK/project/repo" answer how session
    mkdir -p "$project" && (
        cd "$project" &&
            git init -q &&
            printf 'kept\n' >note.txt &&
            git add note.txt &&
            git -c user.name=smoke -c user.email=smoke@example.invalid commit -qm seed &&
            printf 'local change\n' >>note.txt
    ) >"$SMOKE_LOGS/approval.log" 2>&1 || {
        smoke_result approval FAIL "could not prepare a git folder: $(last_line "$SMOKE_LOGS/approval.log")"
        return 1
    }
    # The scripted model asks to run `git reset --hard HEAD` (flagged as destructive). A finite
    # `chat -q` without a terminal has nobody to ask: the run refuses it (single_query_mode) or,
    # through the gateway daemon, leaves the approval waiting for a terminal. Either way nothing
    # may run before a person says yes.
    (cd "$project" && bounded "$SMOKE_TURN_WAIT" "$SMOKE_INDUS" -m approve_or_decline chat -q "please tidy the tree") \
        >>"$SMOKE_LOGS/approval.log" 2>&1
    if ! grep -q 'local change' "$project/note.txt"; then
        smoke_result approval FAIL "the risky command ran without an approval"
        return 1
    fi
    if grep -q 'declined' "$SMOKE_LOGS/approval.log"; then
        how="refused in an unattended run"
    elif grep -q 'waiting for an answer' "$SMOKE_LOGS/approval.log"; then
        how="left waiting for an approval"
    else
        smoke_result approval FAIL "no refusal and no waiting approval: $(last_line "$SMOKE_LOGS/approval.log")"
        return 1
    fi
    if [ "$SMOKE_INTERACTIVE" = true ] && [ -t 0 ]; then
        printf '\nNow approve it by hand: Indus opens the chat; answer the approval with yes, then type /quit.\n'
        session="$(sed -n 's/.*`indus -r \([^`]*\)`.*/\1/p' "$SMOKE_LOGS/approval.log" | head -n 1)"
        if [ -n "$session" ]; then
            (cd "$project" && "$SMOKE_INDUS" -r "$session")
        else
            (cd "$project" && "$SMOKE_INDUS" -m approve_or_decline chat -q "please tidy the tree")
        fi
        printf 'Did Indus ask before running git reset --hard HEAD, and run it after you approved? [y/N] '
        read -r answer
        case "$answer" in
            y | Y | yes) smoke_result approval PASS "$how; asked and ran when a person approved" ;;
            *) smoke_result approval FAIL "the person did not see the approval work"; return 1 ;;
        esac
        return 0
    fi
    smoke_result approval PASS "$how, nothing ran (interactive approval: --interactive)"
}

step_resume() {
    local id
    # Through the daemon the session is a cli one; a --local one-shot is listed under oneshot.
    bounded 60 "$SMOKE_INDUS" sessions list --limit 50 >"$SMOKE_LOGS/sessions.log" 2>&1
    bounded 60 "$SMOKE_INDUS" sessions list --limit 50 --source oneshot >>"$SMOKE_LOGS/sessions.log" 2>&1
    id="$(awk '/remember the blue kite/ { print $1; exit }' "$SMOKE_LOGS/sessions.log")"
    if [ -z "$id" ]; then
        smoke_result resume FAIL "the first session is not listed: $(last_line "$SMOKE_LOGS/sessions.log")"
        return 1
    fi
    (cd "$SMOKE_WORK/project" && bounded "$SMOKE_TURN_WAIT" "$SMOKE_INDUS" -r "$id" -z "what did I ask") \
        >"$SMOKE_LOGS/resume.log" 2>&1
    if grep -q 'You said: what did I ask' "$SMOKE_LOGS/resume.log"; then
        smoke_result resume PASS "session $id resumed"
    else
        smoke_result resume FAIL "$(last_line "$SMOKE_LOGS/resume.log")"
        return 1
    fi
}

step_doctor() {
    if bounded 180 "$SMOKE_INDUS" doctor >"$SMOKE_LOGS/doctor.log" 2>&1; then
        smoke_result doctor PASS "$(last_line "$SMOKE_LOGS/doctor.log")"
    else
        smoke_result doctor FAIL "$(last_line "$SMOKE_LOGS/doctor.log")"
        return 1
    fi
}

SMOKE_DASH_PID=""
SMOKE_DASH_URL=""

smoke_http() { # URL [HEADER]: print the HTTP status (000 when nothing answered)
    if [ $# -ge 2 ]; then
        curl -s -o "$SMOKE_WORK/http.body" -w '%{http_code}' --max-time 20 -H "$2" "$1" 2>/dev/null || :
    else
        curl -s -o "$SMOKE_WORK/http.body" -w '%{http_code}' --max-time 20 "$1" 2>/dev/null || :
    fi
}

step_dashboard_gate() {
    local waited=0 status
    "$SMOKE_INDUS" dashboard --port 0 --no-open </dev/null >"$SMOKE_LOGS/dashboard.log" 2>&1 &
    SMOKE_DASH_PID=$!
    while [ "$waited" -lt "$SMOKE_DASHBOARD_WAIT" ]; do
        SMOKE_DASH_URL="$(sed -n 's#^Dashboard on \(http://127\.0\.0\.1:[0-9]*\)/.*#\1#p' "$SMOKE_LOGS/dashboard.log" | head -n 1)"
        [ -z "$SMOKE_DASH_URL" ] || break
        kill -0 "$SMOKE_DASH_PID" 2>/dev/null || break
        sleep 1
        waited=$((waited + 1))
    done
    if [ -z "$SMOKE_DASH_URL" ]; then
        smoke_result dashboard-gate FAIL "the dashboard did not start: $(last_line "$SMOKE_LOGS/dashboard.log")"
        return 1
    fi
    status="$(smoke_http "$SMOKE_DASH_URL/api/status")"
    if [ "$status" != 200 ]; then
        smoke_result dashboard-gate FAIL "GET /api/status answered $status"
        return 1
    fi
    status="$(smoke_http "$SMOKE_DASH_URL/api/config")"
    if [ "$status" != 401 ]; then
        smoke_result dashboard-gate FAIL "GET /api/config without the token answered $status, not 401"
        return 1
    fi
    smoke_result dashboard-gate PASS "$SMOKE_DASH_URL: status 200, settings 401 without the token"
}

step_dashboard_login() {
    local status token
    if [ -z "$SMOKE_DASH_URL" ]; then
        smoke_result dashboard-login SKIP "the dashboard did not start"
        return 0
    fi
    status="$(smoke_http "$SMOKE_DASH_URL/")"
    token="$(sed -n 's/.*__INDUS_SESSION_TOKEN__=["]\([^"]*\)["].*/\1/p' "$SMOKE_WORK/http.body" | head -n 1)"
    if [ "$status" != 200 ] || [ -z "$token" ]; then
        smoke_result dashboard-login FAIL "the page answered $status without a session token ($(grep -m 1 -i 'not built\|build failed' "$SMOKE_LOGS/dashboard.log" || printf 'see dashboard.log'))"
        stop_child "$SMOKE_DASH_PID"
        return 1
    fi
    status="$(smoke_http "$SMOKE_DASH_URL/api/config" "X-Indus-Session-Token: $token")"
    stop_child "$SMOKE_DASH_PID"
    SMOKE_DASH_PID=""
    if [ "$status" != 200 ]; then
        smoke_result dashboard-login FAIL "the page's token was refused ($status)"
        return 1
    fi
    smoke_result dashboard-login PASS "the page's token opens the API"
}

SMOKE_DMG=""
SMOKE_MOUNT=""
SMOKE_APP=""

step_desktop_download() {
    local name
    if [ "$SMOKE_DESKTOP" = skip ]; then
        smoke_result desktop-download SKIP "--desktop skip"
        return 0
    fi
    if [ "$SMOKE_DESKTOP" != auto ]; then
        SMOKE_DMG="$SMOKE_DESKTOP"
    else
        while IFS= read -r name; do
            if bounded 900 curl -fsSL --proto '=https' --tlsv1.2 -o "$SMOKE_WORK/$name" \
                "$SMOKE_RELEASE_URL/$name" 2>>"$SMOKE_LOGS/desktop-download.log"; then
                SMOKE_DMG="$SMOKE_WORK/$name"
                break
            fi
        done < <(smoke_dmg_names)
    fi
    if [ -z "$SMOKE_DMG" ] || [ ! -f "$SMOKE_DMG" ]; then
        smoke_result desktop-download FAIL "no DMG for $(uname -m) on $SMOKE_RELEASE_URL"
        return 1
    fi
    mkdir -p "$SMOKE_WORK/dmg"
    if ! bounded 300 hdiutil attach -nobrowse -readonly -mountpoint "$SMOKE_WORK/dmg" "$SMOKE_DMG" \
        >"$SMOKE_LOGS/desktop-mount.log" 2>&1; then
        smoke_result desktop-download FAIL "the DMG did not mount: $(last_line "$SMOKE_LOGS/desktop-mount.log")"
        return 1
    fi
    SMOKE_MOUNT="$SMOKE_WORK/dmg"
    # A copy outside the image, so the app runs the way a person installs it.
    mkdir -p "$SMOKE_WORK/Applications"
    if ! ditto "$SMOKE_MOUNT/Indus.app" "$SMOKE_WORK/Applications/Indus.app" 2>"$SMOKE_LOGS/desktop-copy.log"; then
        smoke_result desktop-download FAIL "Indus.app is not in the DMG: $(last_line "$SMOKE_LOGS/desktop-copy.log")"
        return 1
    fi
    SMOKE_APP="$SMOKE_WORK/Applications/Indus.app"
    smoke_result desktop-download PASS "$(basename "$SMOKE_DMG")"
}

step_licences() {
    local missing=""
    head -n 5 "$SMOKE_CHECKOUT/THIRD_PARTY_NOTICES.md" 2>/dev/null | grep -q '^# Third-party notices' ||
        missing="checkout"
    if [ -n "$SMOKE_APP" ]; then
        head -n 5 "$SMOKE_APP/Contents/Resources/THIRD_PARTY_NOTICES.md" 2>/dev/null | grep -q '^# Third-party notices' ||
            missing="${missing:+$missing, }desktop package"
    fi
    if [ -n "$missing" ]; then
        smoke_result licences FAIL "THIRD_PARTY_NOTICES.md missing in: $missing"
        return 1
    fi
    # The desktop app has no licences screen of its own yet (follow-up in the rehearsal log).
    smoke_result licences PASS "notices in the checkout${SMOKE_APP:+ and the app bundle} (no in-app licences screen yet)"
}

step_signature() {
    if [ -z "$SMOKE_APP" ]; then
        smoke_result signature SKIP "no desktop package"
        return 0
    fi
    if ! codesign --verify --deep --strict "$SMOKE_APP" >"$SMOKE_LOGS/signature.log" 2>&1; then
        smoke_result signature FAIL "codesign: $(last_line "$SMOKE_LOGS/signature.log") (unsigned until P23-15)"
        return 1
    fi
    if ! spctl --assess --type execute -vv "$SMOKE_APP" >>"$SMOKE_LOGS/signature.log" 2>&1; then
        smoke_result signature FAIL "Gatekeeper: $(last_line "$SMOKE_LOGS/signature.log")"
        return 1
    fi
    smoke_result signature PASS "$(grep -m 1 'source=' "$SMOKE_LOGS/signature.log" || printf 'accepted')"
}

SMOKE_DESKTOP_PID=""

step_desktop_launch() {
    local answer
    if [ -z "$SMOKE_APP" ]; then
        smoke_result desktop-launch SKIP "no desktop package"
        return 0
    fi
    if [ -n "${SSH_CONNECTION:-}" ]; then
        smoke_result desktop-launch SKIP "an SSH session has no screen (manual: open Indus.app and reach the chat)"
        return 0
    fi
    PATH="$HOME/.local/bin:$PATH" "$SMOKE_APP/Contents/MacOS/Indus" </dev/null >"$SMOKE_LOGS/desktop.log" 2>&1 &
    SMOKE_DESKTOP_PID=$!
    sleep "$SMOKE_DESKTOP_WAIT"
    if ! kill -0 "$SMOKE_DESKTOP_PID" 2>/dev/null; then
        smoke_result desktop-launch FAIL "the app exited: $(last_line "$SMOKE_LOGS/desktop.log")"
        SMOKE_DESKTOP_PID=""
        return 1
    fi
    if [ "$SMOKE_INTERACTIVE" = true ] && [ -t 0 ]; then
        printf 'Did the desktop window open and reach the chat? [y/N] '
        read -r answer
        stop_child "$SMOKE_DESKTOP_PID"
        SMOKE_DESKTOP_PID=""
        case "$answer" in
            y | Y | yes) smoke_result desktop-launch PASS "a person saw the chat" ;;
            *) smoke_result desktop-launch FAIL "the person did not reach the chat"; return 1 ;;
        esac
        return 0
    fi
    stop_child "$SMOKE_DESKTOP_PID"
    SMOKE_DESKTOP_PID=""
    smoke_result desktop-launch SKIP "running after ${SMOKE_DESKTOP_WAIT}s; chat not confirmed (use --interactive)"
}

step_uninstall() {
    if ! bounded 300 "$SMOKE_INDUS" uninstall -y >"$SMOKE_LOGS/uninstall.log" 2>&1; then
        smoke_result uninstall FAIL "$(last_line "$SMOKE_LOGS/uninstall.log")"
        return 1
    fi
    if [ -e "$SMOKE_CHECKOUT" ] || [ -e "$SMOKE_INDUS" ]; then
        smoke_result uninstall FAIL "the code or the launcher is still there"
        return 1
    fi
    if [ ! -f "$SMOKE_DATA/state.db" ] || [ ! -f "$SMOKE_DATA/config.yaml" ]; then
        smoke_result uninstall FAIL "the data was not kept in $SMOKE_DATA"
        return 1
    fi
    smoke_result uninstall PASS "code and launcher removed, sessions and settings kept"
}

# -- the run --------------------------------------------------------------------------------------

smoke_cleanup() {
    stop_child "$SMOKE_DESKTOP_PID"
    stop_child "$SMOKE_DASH_PID"
    SMOKE_DESKTOP_PID=""
    SMOKE_DASH_PID=""
    if [ -n "$SMOKE_MODEL_PID" ]; then
        kill -TERM "$SMOKE_MODEL_PID" 2>/dev/null || :
        wait "$SMOKE_MODEL_PID" 2>/dev/null || :
        exec 7>&- 2>/dev/null || :
        SMOKE_MODEL_PID=""
    fi
    if [ -x "$SMOKE_INDUS" ]; then bounded 60 "$SMOKE_INDUS" gateway stop >/dev/null 2>&1 || :; fi
    if [ -n "$SMOKE_MOUNT" ]; then
        hdiutil detach -quiet "$SMOKE_MOUNT" >/dev/null 2>&1 || :
        SMOKE_MOUNT=""
    fi
}

smoke_main() {
    local step tool needs_install=true
    smoke_parse "$@"
    if [ "$SMOKE_DRY_RUN" = true ]; then
        smoke_plan
        printf 'Dry run: nothing was fetched or installed.\n'
        exit 0
    fi
    for tool in curl git bash hdiutil ditto; do
        command -v "$tool" >/dev/null 2>&1 || { printf '%s is required\n' "$tool" >&2; exit 1; }
    done
    if [ -z "$SMOKE_WORK" ]; then
        SMOKE_WORK="$(mktemp -d "${TMPDIR:-/tmp}/indus-smoke.XXXXXX")" || exit 1
    elif [ -e "$SMOKE_WORK" ] && [ -n "$(ls -A "$SMOKE_WORK" 2>/dev/null)" ]; then
        # The folder is removed at the end: never one that already holds something.
        printf -- '--workdir must be a new or empty folder: %s\n' "$SMOKE_WORK" >&2
        exit 2
    fi
    mkdir -p "$SMOKE_WORK" || exit 1
    SMOKE_WORK="$(cd "$SMOKE_WORK" && pwd -P)"
    SMOKE_LOGS="$SMOKE_WORK/logs"
    smoke_plan
    smoke_isolate
    trap smoke_cleanup EXIT
    for step in "${SMOKE_STEPS[@]}"; do
        if [ "$needs_install" = false ] && [ "$step" != desktop-download ] && [ "$step" != signature ] &&
            [ "$step" != desktop-launch ]; then
            smoke_result "$step" SKIP "the install failed"
            continue
        fi
        case "$step" in
            install) step_install || needs_install=false ;;
            version) step_version ;;
            setup) step_setup ;;
            turn) step_turn ;;
            approval) step_approval ;;
            resume) step_resume ;;
            doctor) step_doctor ;;
            dashboard-gate) step_dashboard_gate ;;
            dashboard-login) step_dashboard_login ;;
            desktop-download) step_desktop_download ;;
            licences) step_licences ;;
            signature) step_signature ;;
            desktop-launch) step_desktop_launch ;;
            uninstall) step_uninstall ;;
        esac
    done
    smoke_cleanup
    trap - EXIT
    printf '\nSummary (%s, %s): %s failed\n%s' "$SMOKE_OS" "$SMOKE_TAG" "$SMOKE_FAILED" "$SMOKE_RESULTS"
    if [ "$SMOKE_FAILED" -eq 0 ] && [ "$SMOKE_KEEP" = false ]; then
        rm -rf "$SMOKE_WORK"
        printf 'Every step passed or was skipped; the work folder was removed.\n'
        exit 0
    fi
    printf 'Logs: %s\n' "$SMOKE_LOGS"
    [ "$SMOKE_FAILED" -eq 0 ] || exit 1
    exit 0
}

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    smoke_main "$@"
fi
