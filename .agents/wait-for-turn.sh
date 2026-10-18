#!/usr/bin/env bash
# wait-for-turn.sh — wait for this agent's turn in the two-agent protocol without spending AI tokens.
#
# Usage: wait-for-turn.sh <ME> [BRANCH] [MAX_SECONDS] [POLL_SECONDS]
#   ME            this agent's name as written in turn.json, e.g. AGENT_1 (its status file: agent-1.json)
#   BRANCH        the work branch to watch      (default: $BRANCH, else main)
#   MAX_SECONDS   stop and exit 2 after this     (default: $MAX, else 540)
#   POLL_SECONDS  pause between two polls        (default: $POLL, else 180)
# Env: REMOTE (default origin), AGENTS_DIR (default .agents), STALE_MIN (default 60),
#      DISPLAY_TZ (zone for printed times, e.g. Asia/Kolkata; default $TZ, else UTC).
#
# Run it from inside the repository, in the background (it sleeps between polls). Each poll fetches
# BRANCH and reads the lock and the status files from the remote copy with `git show`, so the working
# tree is never touched (builders may still be writing there). Prints one usage line per agent.
#
# Exit: 0 my turn | 2 still waiting, run again | 3 turn holder stale | 4 DONE | 1 error
set -euo pipefail

ME="${1:-}"
if [ -z "$ME" ] || [ "$ME" = "-h" ] || [ "$ME" = "--help" ]; then
  sed -n '2,16p' "$0"
  exit 1
fi
BRANCH="${2:-${BRANCH:-main}}"
MAX_SECONDS="${3:-${MAX:-540}}"
POLL_SECONDS="${4:-${POLL:-180}}"
REMOTE="${REMOTE:-origin}"
AGENTS_DIR="${AGENTS_DIR:-.agents}"
AGENTS_DIR="${AGENTS_DIR%/}"
STALE_MIN="${STALE_MIN:-60}"
DISPLAY_TZ="${DISPLAY_TZ:-${TZ:-UTC}}"
UPSTREAM="refs/remotes/$REMOTE/$BRANCH"

# json_get <json text> <key>: the value of a top-level key (string or number), empty when absent.
json_get() {
  local json="$1" key="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$json" | jq -r --arg k "$key" '.[$k] // empty' 2>/dev/null && return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$json" | python3 -I -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1])
except Exception:
    v = None
print("" if v is None else v)' "$key" 2>/dev/null && return 0
  fi
  # Last resort for flat JSON whose values hold no escaped quotes.
  printf '%s' "$json" | tr '\n' ' ' |
    grep -oE "\"$key\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|[-0-9.]+)" | head -n 1 |
    sed -E 's/^"[^"]*"[[:space:]]*:[[:space:]]*"?([^"]*)"?$/\1/' || true
}

# remote_file <name>: a file of AGENTS_DIR as pushed to the remote branch, empty when missing.
remote_file() { git show "$UPSTREAM:$AGENTS_DIR/$1" 2>/dev/null || true; }

# status_file <AGENT_N>: AGENT_1 -> agent-1.json
status_file() { printf 'agent-%s.json' "$(printf '%s' "${1#AGENT_}" | tr 'A-Z_' 'a-z-')"; }

# fmt_time <ISO 8601>: "HH:MM ZONE" in DISPLAY_TZ; the raw text where `date -d` cannot parse it.
fmt_time() {
  if [ -z "$1" ]; then echo unknown; return; fi
  TZ="$DISPLAY_TZ" date -d "$1" '+%H:%M %Z' 2>/dev/null || echo "$1"
}

# age_min <ISO 8601>: minutes since then, -1 when unknown.
age_min() {
  local t
  t=$(date -d "$1" +%s 2>/dev/null) || { echo -1; return; }
  echo $(( ($(date +%s) - t) / 60 ))
}

pct() { if [ -n "$1" ]; then echo "$1%"; else echo unknown; fi; }

usage_line() {
  local file="$1" json n state step
  n="${file#agent-}"; n="${n%.json}"
  json=$(remote_file "$file")
  if [ -z "$json" ]; then echo "📊 Agent $n usage: unknown (no $AGENTS_DIR/$file on $REMOTE/$BRANCH)"; return; fi
  state=$(json_get "$json" state); step=$(json_get "$json" current_step)
  echo "📊 Agent $n usage: $(pct "$(json_get "$json" usage_pct)") used, $(pct "$(json_get "$json" remaining_pct)") left" \
    "— resets $(fmt_time "$(json_get "$json" resets_at)") — ${state:-unknown}${step:+ (step $step)}"
}

list_status_files() {
  git ls-tree --name-only "$UPSTREAM" "$AGENTS_DIR/" 2>/dev/null |
    sed -n "s#^$AGENTS_DIR/\(agent-[^/]*\.json\)\$#\1#p" || true
}

START=$(date +%s)
while :; do
  git fetch -q "$REMOTE" "+refs/heads/$BRANCH:$UPSTREAM" || { echo "git fetch of $REMOTE/$BRANCH failed" >&2; exit 1; }
  TURN_JSON=$(remote_file turn.json)
  if [ -z "$TURN_JSON" ]; then echo "no $AGENTS_DIR/turn.json on $REMOTE/$BRANCH" >&2; exit 1; fi
  TURN=$(json_get "$TURN_JSON" turn)
  STATUS=$(json_get "$TURN_JSON" status)
  if [ -z "$TURN" ] || [ -z "$STATUS" ]; then echo "turn.json has no turn/status" >&2; exit 1; fi

  HOLDER_AGE=$(age_min "$(json_get "$(remote_file "$(status_file "$TURN")")" last_check)")
  AGE_TEXT="${HOLDER_AGE}m ago"; [ "$HOLDER_AGE" -lt 0 ] && AGE_TEXT="at an unknown time"
  echo "[$(TZ="$DISPLAY_TZ" date '+%H:%M %Z')] turn: $TURN ($STATUS) — $TURN last wrote its status $AGE_TEXT"
  for f in $(list_status_files); do usage_line "$f"; done

  if [ "$STATUS" = "DONE" ]; then exit 4; fi
  if [ "$TURN" = "$ME" ]; then exit 0; fi
  if { [ "$STATUS" = "ACTIVE" ] || [ "$STATUS" = "PAUSED" ]; } && [ "$HOLDER_AGE" -ge "$STALE_MIN" ]; then
    echo "stale: $TURN holds the turn ($STATUS) and has not written its status for ${HOLDER_AGE} min" >&2
    exit 3
  fi
  if [ $(( $(date +%s) - START )) -ge "$MAX_SECONDS" ]; then exit 2; fi
  sleep "$POLL_SECONDS"
done
