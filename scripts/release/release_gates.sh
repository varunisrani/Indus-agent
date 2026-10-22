#!/usr/bin/env bash
# P25-21: run the release gates on a fresh clone of the candidate commit.
#
#   scripts/release/release_gates.sh <repo> <sha> <gates file>
#
# <repo> is a clone URL or a local repository path; <sha> a commit id (abbreviated ids are
# resolved in the clone); <gates file> holds one gate per line, `<name> <command>` (blank lines
# and lines starting with `#` are skipped; names are [a-z0-9][a-z0-9_-]* and unique).
#
# The work folder is $WORK_DIR when set (it must be empty or absent), else a new folder from
# `mktemp -d`. The repository is cloned into <work>/clone without sharing objects with a local
# source (so nothing ignored, untracked or cached there comes along) and the commit is checked
# out detached. Each gate then runs with `bash -c` from the clone's root, stdin closed, with
# GATE, LOG_DIR, CLONE_DIR, WORK_DIR and RELEASE_SHA exported, and all of its output in
# <work>/logs/<name>.log. Every gate runs, also after a failure. One line per gate,
# `<name> exit=<code> <seconds>s`, goes to standard output and to <work>/gates.txt; <work>/run.txt
# records the repository, the commit and the times.
#
# Exit: 0 every gate exited 0; 1 at least one gate failed; 2 a setup problem before any gate ran
# (arguments, gates file, work folder, clone or checkout).

set -u

PROG=release_gates

die() {
	echo "$PROG: $*" >&2
	exit 2
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ "$#" -ne 3 ]; then
	echo "usage: $0 <repo> <sha> <gates file>" >&2
	exit 2
fi
repo=$1
sha=$2
gates_file=$3

[ -n "$repo" ] || die "the repository is empty"
[[ $sha =~ ^[0-9a-fA-F]{4,40}$ ]] || die "not a commit id: $sha"
[ -f "$gates_file" ] && [ -r "$gates_file" ] || die "cannot read the gates file: $gates_file"

# --- Read the whole gates file before anything is cloned: a bad file costs nothing. ---
names=()
commands=()
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
	lineno=$((lineno + 1))
	line=${line%$'\r'}
	line=${line#"${line%%[![:space:]]*}"}
	case $line in
	'' | '#'*) continue ;;
	esac
	name=${line%%[[:space:]]*}
	command=${line#"$name"}
	command=${command#"${command%%[![:space:]]*}"}
	[[ $name =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "$gates_file:$lineno: bad gate name: $name"
	[ -n "$command" ] || die "$gates_file:$lineno: gate $name has no command"
	for seen in "${names[@]+"${names[@]}"}"; do
		[ "$seen" != "$name" ] || die "$gates_file:$lineno: duplicate gate: $name"
	done
	names+=("$name")
	commands+=("$command")
done <"$gates_file"
[ "${#names[@]}" -gt 0 ] || die "no gates in $gates_file"

# --- A new, empty work folder. ---
if [ -n "${WORK_DIR:-}" ]; then
	mkdir -p -- "$WORK_DIR" || die "cannot create the work folder: $WORK_DIR"
	[ -z "$(ls -A -- "$WORK_DIR")" ] || die "the work folder is not empty: $WORK_DIR"
else
	WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/indus-release-gates.XXXXXX") ||
		die "cannot create a work folder"
fi
WORK_DIR=$(cd -- "$WORK_DIR" && pwd -P) || die "cannot enter the work folder"
clone_dir=$WORK_DIR/clone
log_dir=$WORK_DIR/logs
summary=$WORK_DIR/gates.txt
setup_log=$WORK_DIR/setup.log
mkdir -p -- "$log_dir" || die "cannot create $log_dir"

# --- Fresh clone at the commit. ---
started=$(utc_now)
if ! git clone --quiet --no-local -- "$repo" "$clone_dir" >"$setup_log" 2>&1; then
	cat "$setup_log" >&2
	die "clone failed: $repo"
fi
if ! full_sha=$(git -C "$clone_dir" rev-parse --verify --quiet "$sha^{commit}"); then
	die "commit $sha is not in $repo"
fi
if ! git -C "$clone_dir" checkout --quiet --detach "$full_sha" >>"$setup_log" 2>&1; then
	cat "$setup_log" >&2
	die "checkout of $full_sha failed"
fi

{
	echo "repository $repo"
	echo "commit $full_sha"
	echo "gates_file $gates_file"
	echo "gates ${#names[@]}"
	echo "started $started"
} >"$WORK_DIR/run.txt"

trap 'echo "$PROG: interrupted" >&2; exit 130' INT TERM

# --- Every gate, in order, whatever the previous one did. ---
: >"$summary"
failed=0
i=0
while [ "$i" -lt "${#names[@]}" ]; do
	name=${names[$i]}
	command=${commands[$i]}
	begin=$(date +%s)
	(
		cd -- "$clone_dir" || exit 2
		exec env GATE="$name" LOG_DIR="$log_dir" CLONE_DIR="$clone_dir" WORK_DIR="$WORK_DIR" \
			RELEASE_SHA="$full_sha" bash -c "$command"
	) </dev/null >"$log_dir/$name.log" 2>&1
	code=$?
	end=$(date +%s)
	line="$name exit=$code $((end - begin))s"
	echo "$line" >>"$summary"
	echo "$line"
	[ "$code" -eq 0 ] || failed=$((failed + 1))
	i=$((i + 1))
done

{
	echo "finished $(utc_now)"
	echo "failed $failed"
} >>"$WORK_DIR/run.txt"

echo "$PROG: $failed of ${#names[@]} gates failed at $full_sha; logs in $log_dir"
[ "$failed" -eq 0 ] || exit 1
exit 0
