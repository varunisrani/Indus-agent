# shellcheck shell=bash
# Indus Agent installer for Linux and macOS: core (P23-03).
#
#   curl -fsSL <release>/install.sh | bash
#   bash install.sh [--channel stable|canary|main] [--dir PATH] [--indus-home PATH] ...
#
# The stage protocol the desktop's in-app runner drives (P20-09):
#   --manifest            one JSON line: {"protocol_version":1,"stages":[{name,title,category,
#                         needs_user_input}]}
#   --stage NAME --json   run one stage; the last stdout line is {"ok","stage","skipped","reason"?}
#   --non-interactive     stages that need a person (setup, gateway) are skipped with a reason
#
# Without --stage the whole ladder runs and one local receipt is written under
# <home>/logs/install_receipts/ (never uploaded). Sourcing this file defines functions and
# constants only; nothing runs until ins_main.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "install.sh needs bash: curl -fsSL <url>/install.sh | bash" >&2
    exit 1
fi

# -- arguments ------------------------------------------------------------------------------------

ins_usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --channel stable|canary|main  what to install (default: the newest stable release; forks and
                                other branches follow their branch). Env: INDUS_INSTALL_CHANNEL.
  --branch NAME                 branch to clone or follow (default: main)
  --commit SHA                  install exactly this commit (it must be on the branch)
  --dir PATH                    where the code goes (default: <home>/indus-agent)
  --indus-home PATH             the Indus home (default: $INDUS_HOME or ~/.indus)
  --non-interactive             never ask; skip provider and gateway setup (alias --skip-setup)
  --skip-browser                leave out the browser tools (remembered by later installs)
  --skip-computer-use           leave out the computer-use tools
  --include-desktop             also set up the desktop app
  --verbose                     show every command's output (it always goes to the install log)
  --manifest                    print the stage list as JSON and exit
  --stage NAME [--json]         run one stage (with --json: print its result frame)
  -h, --help                    this help

PowerShell spellings work too (-Channel, -IndusHome, -NonInteractive, ...).
EOF
}

ins_need_value() {
    printf '%s needs a value\n' "$1" >&2
    exit 2
}

ins_parse_args() {
    IN_CHANNEL="${INDUS_INSTALL_CHANNEL:-}"
    IN_BRANCH=main
    IN_COMMIT=""
    IN_DIR=""
    IN_HOME="${INDUS_HOME:-}"
    IN_STAGE=""
    IN_MANIFEST=false
    IN_JSON=false
    IN_NONINTERACTIVE=false
    IN_DESKTOP=false
    IN_SKIP_BROWSER=false
    IN_SKIP_CU=false
    IN_VERBOSE=false
    local opt value inline
    while [ $# -gt 0 ]; do
        opt="$1"
        value=""
        inline=false
        case "$opt" in
            --*=*)
                value="${opt#*=}"
                opt="${opt%%=*}"
                inline=true
                ;;
        esac
        case "$opt" in
            --channel | -Channel | --branch | -Branch | --commit | -Commit | --dir | -InstallDir | \
                --indus-home | -IndusHome | --stage | -Stage)
                if [ "$inline" = false ]; then
                    if [ $# -lt 2 ]; then ins_need_value "$opt"; fi
                    value="$2"
                    case "$value" in "" | -*) ins_need_value "$opt" ;; esac
                    shift
                elif [ -z "$value" ]; then
                    ins_need_value "$opt"
                fi
                case "$opt" in
                    --channel | -Channel) IN_CHANNEL="$value" ;;
                    --branch | -Branch) IN_BRANCH="$value" ;;
                    --commit | -Commit) IN_COMMIT="$value" ;;
                    --dir | -InstallDir) IN_DIR="$value" ;;
                    --indus-home | -IndusHome) IN_HOME="$value" ;;
                    --stage | -Stage) IN_STAGE="$value" ;;
                esac
                ;;
            --manifest | -Manifest | --json | -Json | --non-interactive | -NonInteractive | \
                --skip-setup | -SkipSetup | --include-desktop | -IncludeDesktop | \
                --skip-browser | -SkipBrowser | --skip-computer-use | -SkipComputerUse | \
                --verbose | -Verbose)
                if [ "$inline" = true ]; then
                    printf '%s takes no value\n' "$opt" >&2
                    exit 2
                fi
                case "$opt" in
                    --manifest | -Manifest) IN_MANIFEST=true ;;
                    --json | -Json) IN_JSON=true ;;
                    --non-interactive | -NonInteractive | --skip-setup | -SkipSetup) IN_NONINTERACTIVE=true ;;
                    --include-desktop | -IncludeDesktop) IN_DESKTOP=true ;;
                    --skip-browser | -SkipBrowser) IN_SKIP_BROWSER=true ;;
                    --skip-computer-use | -SkipComputerUse) IN_SKIP_CU=true ;;
                    --verbose | -Verbose) IN_VERBOSE=true ;;
                esac
                ;;
            -h | --help | -Help)
                ins_usage
                exit 0
                ;;
            *)
                printf 'unknown option: %s (see --help)\n' "$1" >&2
                exit 1
                ;;
        esac
        shift
    done
    case "$IN_CHANNEL" in
        "" | stable | canary | main) ;;
        *)
            printf -- '--channel must be stable, canary or main, not %s\n' "$IN_CHANNEL" >&2
            exit 2
            ;;
    esac
    case "$IN_COMMIT" in
        "" | [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
        *)
            printf -- '--commit must be a hex commit id, not %s\n' "$IN_COMMIT" >&2
            exit 2
            ;;
    esac
    [ -n "$IN_HOME" ] || IN_HOME="$HOME/.indus"
    case "$IN_HOME" in /*) ;; *) IN_HOME="$PWD/$IN_HOME" ;; esac
    [ -n "$IN_DIR" ] || IN_DIR="$IN_HOME/indus-agent"
    case "$IN_DIR" in /*) ;; *) IN_DIR="$PWD/$IN_DIR" ;; esac
}

# -- output ---------------------------------------------------------------------------------------

ins_setup_output() {
    INS_LOG="$IN_HOME/logs/install.log"
    # A CI transcript is the only record a failed CI install leaves: show the children there too.
    if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then IN_VERBOSE=true; fi
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        INS_C_INFO=$'\033[36m' INS_C_OK=$'\033[32m' INS_C_WARN=$'\033[33m' INS_C_ERR=$'\033[31m'
        INS_C_DIM=$'\033[2m' INS_C_OFF=$'\033[0m'
    else
        INS_C_INFO="" INS_C_OK="" INS_C_WARN="" INS_C_ERR="" INS_C_DIM="" INS_C_OFF=""
    fi
}

ins_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ins_stamp() { date -u +%Y%m%dT%H%M%SZ; }

ins_info() { printf '%s==>%s %s\n' "$INS_C_INFO" "$INS_C_OFF" "$*"; }
ins_ok() { printf '%s ok%s %s\n' "$INS_C_OK" "$INS_C_OFF" "$*"; }
ins_warn() { printf '%swarning:%s %s\n' "$INS_C_WARN" "$INS_C_OFF" "$*" >&2; }
ins_error() { printf '%serror:%s %s\n' "$INS_C_ERR" "$INS_C_OFF" "$*" >&2; }

# A line for the install log only (and the terminal with --verbose).
ins_log() {
    if [ "${IN_VERBOSE:-false}" = true ]; then printf '%s%s%s\n' "$INS_C_DIM" "$*" "$INS_C_OFF"; fi
    { mkdir -p "${INS_LOG%/*}" && printf '%s\n' "$*" >>"$INS_LOG"; } 2>/dev/null || :
}

# ins_fail MESSAGE [CLASS]: end the running stage as failed. CLASS is a closed token for the
# receipt (never the message: messages carry paths).
ins_fail() {
    INS_REASON="$1"
    INS_CLASS="${2:-other}"
    ins_error "$1"
    exit 1
}

# ins_skip REASON: end the running stage as skipped (it counts as passed).
ins_skip() {
    INS_SKIPPED=true
    INS_REASON="$1"
    ins_info "skipped: $1"
    exit 0
}

# Reads a failed command's output on stdin and prints the line that explains it: git's opening
# `fatal:` or `error:` wins, otherwise whatever non-blank line came last.
ins_error_line() {
    local line pick="" last=""
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        line="${line##*$'\r'}"
        case "$line" in *[![:space:]]*) ;; *) continue ;; esac
        last="$line"
        if [ -z "$pick" ]; then
            case "$line" in fatal:* | error:* | *" fatal: "* | *" error: "*) pick="$line" ;; esac
        fi
    done
    pick="${pick:-$last}"
    printf '%s' "${pick:0:200}"
}

# ins_run [--may-fail] LABEL COMMAND...: run COMMAND with its output appended to the install log
# (and shown with --verbose). On failure, INS_LAST_ERROR holds its telling line and, unless
# --may-fail, the tail of its output goes to stderr. Returns COMMAND's status.
ins_run() {
    local tolerate=false
    if [ "$1" = --may-fail ]; then
        tolerate=true
        shift
    fi
    local label="$1"
    shift
    local log="$INS_LOG" start=0 rc
    INS_LAST_ERROR=""
    if ! { mkdir -p "${log%/*}" && : >>"$log"; } 2>/dev/null; then log=""; fi
    if [ -n "$log" ]; then
        start=$(wc -l <"$log" | tr -d ' ')
        printf '==> %s (%s)\n' "$label" "$(ins_now)" >>"$log"
    fi
    if [ "${IN_VERBOSE:-false}" = true ]; then
        ins_info "$label"
        if [ -n "$log" ]; then
            "$@" </dev/null 2>&1 | tee -a "$log"
            rc=${PIPESTATUS[0]}
        else
            "$@" </dev/null
            rc=$?
        fi
    elif [ -n "$log" ]; then
        "$@" </dev/null >>"$log" 2>&1
        rc=$?
    else
        "$@" </dev/null >/dev/null 2>&1
        rc=$?
    fi
    if [ "$rc" -ne 0 ] && [ -n "$log" ]; then
        INS_LAST_ERROR="$(tail -n +"$((start + 2))" "$log" | ins_error_line)"
        if [ "$tolerate" = false ]; then
            ins_error "$label failed (exit $rc)"
            tail -n +"$((start + 2))" "$log" | tail -n 15 | sed 's/^/    /' >&2
            printf '    full log: %s\n' "$log" >&2
        fi
    fi
    return "$rc"
}

# ": <cause>" for a failure message when the last ins_run left one.
ins_cause() { [ -z "${INS_LAST_ERROR:-}" ] || printf ': %s' "$INS_LAST_ERROR"; }

# The injectable pause between retries (tests replace it).
ins_sleep() { sleep "$1"; }

# -- JSON -----------------------------------------------------------------------------------------

ins_json_string() {
    local value="$1" code char
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    for code in 1 2 3 4 5 6 7 8 11 12 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31; do
        # shellcheck disable=SC2059 # the format is the escape for one control character
        printf -v char "\\$(printf '%03o' "$code")"
        case "$value" in *"$char"*) value="${value//"$char"/$(printf '\\u%04x' "$code")}" ;; esac
    done
    printf '"%s"' "$value"
}

# ins_frame OK STAGE SKIPPED REASON: the result frame of one stage (P20-09 contract).
ins_frame() {
    local frame
    frame="{\"ok\":$1,\"stage\":$(ins_json_string "$2"),\"skipped\":$3"
    if [ -n "${4:-}" ]; then frame="$frame,\"reason\":$(ins_json_string "$4")"; fi
    printf '%s}\n' "$frame"
}

ins_manifest() {
    if [ "$IN_DESKTOP" = true ]; then
        printf '%s\n' "$INS_MANIFEST_DESKTOP"
    else
        printf '%s\n' "$INS_MANIFEST"
    fi
}

ins_is_stage() {
    case " $INS_STAGES " in *" $1 "*) return 0 ;; esac
    return 1
}

ins_needs_input() {
    case " $INS_INPUT_STAGES " in *" $1 "*) return 0 ;; esac
    return 1
}

# -- receipts -------------------------------------------------------------------------------------

# ins_receipt OUTCOME [FAILED_STAGE FAILURE_CLASS]: the one local receipt of a full-ladder run.
# Written through a temp file and an atomic no-clobber link, so the first writer wins and every
# later call is a no-op. Best effort: a receipt never fails the install.
ins_receipt() {
    [ -n "${INS_RUN_ID:-}" ] || return 0
    local dir="$IN_HOME/logs/install_receipts" final tmp body
    final="$dir/$INS_RUN_ID.json"
    {
        [ ! -e "$final" ] || return 0
        mkdir -p "$dir" || return 0
        tmp="$dir/.$INS_RUN_ID.$$.tmp"
        body="{\"id\":\"$INS_RUN_ID\",\"installer\":\"sh\",\"outcome\":$(ins_json_string "$1")"
        if [ -n "${2:-}" ]; then
            body="$body,\"failed_stage\":$(ins_json_string "$2")"
            body="$body,\"failure_class\":$(ins_json_string "${3:-other}")"
        fi
        body="$body,\"started_at\":\"$INS_STARTED_AT\",\"finished_at\":\"$(ins_now)\"}"
        if printf '%s\n' "$body" >"$tmp"; then
            ln "$tmp" "$final" || { [ -e "$final" ] || mv -f "$tmp" "$final"; }
        fi
        rm -f "$tmp"
    } >/dev/null 2>&1
    return 0
}

ins_new_run_id() {
    local id
    id="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    if [ "${#id}" -ne 32 ]; then
        id="$(printf '%08x%08x%08x%08x' "$RANDOM$RANDOM" "$$" "$(date +%s)" "$RANDOM")"
    fi
    printf '%s' "${id:0:32}"
}

# -- platform -------------------------------------------------------------------------------------

ins_check_platform() {
    if [ -n "${TERMUX_VERSION:-}" ] || case "${PREFIX:-}" in *com.termux*) true ;; *) false ;; esac; then
        ins_fail "Termux is not supported by install.sh; a Termux package is planned for a later release" unsupported_platform
    fi
    case "$(uname -s 2>/dev/null)" in
        Linux | Darwin) ;;
        *) ins_fail "install.sh supports Linux and macOS; on Windows run install.ps1 in PowerShell instead" unsupported_platform ;;
    esac
}

# -- stages ---------------------------------------------------------------------------------------

# Ctrl-C reaches the whole process group, so a stage sees it too. When the command it was running
# died of it, the stage ends with that same status; when the command survived (it handled the
# signal itself), its status stands and the interruption is only written down for the ladder.
ins_stage_signal() {
    [ -z "${INS_RUN_DIR:-}" ] || : >"$INS_RUN_DIR/interrupted"
    if [ "$1" -eq "$2" ]; then exit "$2"; fi
}

ins_stage_exit() {
    local rc="$1" ok=false
    [ "$rc" -ne 0 ] || ok=true
    if [ "$ok" = false ] && [ -z "${INS_REASON:-}" ]; then INS_REASON="stage failed (exit $rc)"; fi
    if [ "$IN_JSON" = true ] && [ -n "$IN_STAGE" ]; then
        ins_frame "$ok" "$INS_STAGE_NAME" "${INS_SKIPPED:-false}" "${INS_REASON:-}"
    fi
    if [ -n "${INS_RUN_DIR:-}" ]; then
        printf '%s\n' "${INS_CLASS:-other}" >"$INS_RUN_DIR/class" 2>/dev/null || :
        printf '%s\n' "${INS_SKIPPED:-false}" >"$INS_RUN_DIR/skipped" 2>/dev/null || :
        printf '%s\n' "${INS_REASON:-}" >"$INS_RUN_DIR/reason" 2>/dev/null || :
    fi
    if [ -n "${INS_CLEANUP:-}" ]; then rm -rf "$INS_CLEANUP" 2>/dev/null || :; fi
}

# ins_run_stage NAME: one stage in a subshell; its exit status is the stage's.
ins_run_stage() {
    (
        INS_STAGE_NAME="$1"
        INS_REASON=""
        INS_CLASS=""
        INS_SKIPPED=false
        INS_CLEANUP=""
        trap 'ins_stage_exit "$?"' EXIT
        if [ "${INS_LADDER:-false}" = true ]; then
            trap 'ins_stage_signal "$?" 130' INT
            trap 'ins_stage_signal "$?" 143' TERM
            trap 'ins_stage_signal "$?" 129' HUP
        fi
        if ! ins_is_stage "$1"; then
            INS_REASON="unknown stage: $1"
            INS_CLASS=usage
            printf '%s\n' "$INS_REASON" >&2
            exit 2
        fi
        ins_check_platform
        if ins_needs_input "$1" && [ "$IN_NONINTERACTIVE" = true ]; then
            ins_skip "needs user input (--non-interactive)"
        fi
        "ins_stage_$1"
        exit 0
    )
}

# -- the ladder -----------------------------------------------------------------------------------

ins_on_signal() {
    INS_SIGNAL="$1"
    if [ "${INS_IN_STAGE:-false}" = false ]; then ins_die_interrupted "${INS_CURRENT:-prerequisites}"; fi
}

# Record the interruption, then die by the signal itself so the caller sees its status.
ins_die_interrupted() {
    local sig="${INS_SIGNAL:-INT}" code=130
    case "$sig" in HUP) code=129 ;; TERM) code=143 ;; esac
    ins_receipt interrupted "$1" interrupted
    [ -z "${INS_RUN_DIR:-}" ] || rm -rf "$INS_RUN_DIR"
    trap - INT TERM HUP EXIT
    kill -s "$sig" "$$" 2>/dev/null
    exit "$code"
}

ins_ladder() {
    local name rc index=0 total skipped reason
    INS_LADDER=true
    INS_SIGNAL=""
    INS_IN_STAGE=false
    INS_CURRENT=""
    INS_STARTED_AT="$(ins_now)"
    INS_RUN_ID="$(ins_new_run_id)"
    INS_RUN_DIR="$(mktemp -d 2>/dev/null)" || INS_RUN_DIR=""
    # shellcheck disable=SC2086 # the stage list is one word per stage
    set -- $INS_STAGES
    total=$#
    trap 'ins_on_signal INT' INT
    trap 'ins_on_signal TERM' TERM
    trap 'ins_on_signal HUP' HUP
    ins_info "Installing Indus Agent into $IN_DIR (home $IN_HOME)"
    for name in "$@"; do
        index=$((index + 1))
        # After a TERM or HUP no further stage starts.
        if [ -n "$INS_SIGNAL" ]; then ins_die_interrupted "$name"; fi
        INS_CURRENT="$name"
        [ -z "$INS_RUN_DIR" ] || rm -f "$INS_RUN_DIR"/class "$INS_RUN_DIR"/skipped "$INS_RUN_DIR"/reason
        ins_info "[$index/$total] $(ins_stage_title "$name")"
        INS_IN_STAGE=true
        ins_run_stage "$name"
        rc=$?
        INS_IN_STAGE=false
        if [ "$rc" -eq 0 ]; then
            skipped="$(cat "$INS_RUN_DIR/skipped" 2>/dev/null)"
            reason="$(cat "$INS_RUN_DIR/reason" 2>/dev/null)"
            if [ "$skipped" = true ]; then ins_ok "skipped ($reason)"; else ins_ok "$name"; fi
            # The stage finished, so a Ctrl-C it saw was handled there: carry on.
            if [ "$INS_SIGNAL" = INT ]; then INS_SIGNAL=""; fi
            continue
        fi
        case "$rc" in
            129) INS_SIGNAL="${INS_SIGNAL:-HUP}" ;;
            130) INS_SIGNAL="${INS_SIGNAL:-INT}" ;;
            143) INS_SIGNAL="${INS_SIGNAL:-TERM}" ;;
        esac
        if [ -z "$INS_SIGNAL" ] && [ -n "$INS_RUN_DIR" ] && [ -e "$INS_RUN_DIR/interrupted" ]; then
            INS_SIGNAL=INT
        fi
        if [ -n "$INS_SIGNAL" ]; then ins_die_interrupted "$name"; fi
        ins_receipt failed "$name" "$(cat "$INS_RUN_DIR/class" 2>/dev/null || echo other)"
        [ -z "$INS_RUN_DIR" ] || rm -rf "$INS_RUN_DIR"
        ins_error "the install stopped at '$name'; details in $INS_LOG"
        exit "$rc"
    done
    ins_receipt success
    [ -z "$INS_RUN_DIR" ] || rm -rf "$INS_RUN_DIR"
    if [ -n "$INS_SIGNAL" ]; then ins_die_interrupted complete; fi
    trap - INT TERM HUP
    ins_path_hint
}

ins_main() {
    ins_parse_args "$@"
    ins_setup_output
    export UV_NO_CONFIG=1
    export INDUS_HOME="$IN_HOME"
    if [ "$IN_MANIFEST" = true ]; then
        ins_manifest
        exit 0
    fi
    if [ -n "$IN_STAGE" ]; then
        INS_LADDER=false
        ins_run_stage "$IN_STAGE"
        exit $?
    fi
    ins_ladder
    exit 0
}
