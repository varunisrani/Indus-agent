# shellcheck shell=bash
# Indus Agent installer: the repository stage (P23-04, R10.14).
#
# A fresh install clones into a hidden sibling staging directory and renames the finished tree
# into place, so an interrupted or failed clone never leaves a target directory behind. An existing
# checkout moves forward on its channel; local edits are stashed and commits that a reset would
# drop are saved under refs/indus/install-backups/ first.

ins_repo_url() { printf '%s' "${INDUS_INSTALL_REPO_URL:-https://github.com/$INS_REPO_SLUG.git}"; }

# Whether URL names our own repository (forks and mirrors follow their branch by default).
ins_is_official() {
    local url slug
    url="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    slug="$(printf '%s' "$INS_REPO_SLUG" | tr '[:upper:]' '[:lower:]')"
    url="${url%/}"
    url="${url%.git}"
    case "$url" in
        "https://github.com/$slug" | "git@github.com:$slug" | "ssh://git@github.com/$slug") return 0 ;;
    esac
    return 1
}

ins_sha256_text() {
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$1" | sha256sum | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1
    else
        return 1
    fi
}

# The id an install's settings are kept under: the first 16 hex of SHA-256 of its real path.
ins_install_id() {
    local real digest
    real="$(cd "$1" 2>/dev/null && pwd -P)" || return 1
    digest="$(ins_sha256_text "$real")" || return 1
    printf '%s' "${digest:0:16}"
}

# ins_install_setting DIR NAME: updates.installs.<id>.<NAME> from <home>/config.yaml (the schema
# key `indus update` reads, P23-17), read without Python from the block YAML the settings writer
# produces. A scalar prints as it is; a list (block `- item` lines or `[a, b]`) prints as its
# items separated by spaces. Prints nothing when absent or unreadable.
ins_install_setting() {
    local config="$IN_HOME/config.yaml" id
    [ -f "$config" ] || return 0
    id="$(ins_install_id "$1")" || return 0
    awk -v id="$id" -v want="$2" -v q="'" '
        function clean(s,   f) {
            sub(/^[ \t]+/, "", s)
            sub(/[ \t]+#.*$/, "", s)
            sub(/[ \t\r]+$/, "", s)
            f = substr(s, 1, 1)
            if (length(s) >= 2 && (f == "\"" || f == q) && substr(s, length(s), 1) == f) {
                s = substr(s, 2, length(s) - 2)
            }
            return s
        }
        function flow(s,   n, i, parts, out) {
            s = substr(s, 2, length(s) - 2)
            n = split(s, parts, ",")
            out = ""
            for (i = 1; i <= n; i++) {
                parts[i] = clean(parts[i])
                if (parts[i] != "") out = out (out == "" ? "" : " ") parts[i]
            }
            return out
        }
        /^[ \t]*(#|\r?$)/ { next }
        collecting {
            match($0, /^ */)
            if (RLENGTH >= keyindent && substr($0, RLENGTH + 1, 2) == "- ") {
                items = items (items == "" ? "" : " ") clean(substr($0, RLENGTH + 3))
                next
            }
            print items
            done = 1
            exit
        }
        {
            match($0, /^ */)
            indent = RLENGTH
            rest = substr($0, indent + 1)
            colon = index(rest, ":")
            if (colon == 0) next
            while (depth > 0 && level[depth] >= indent) depth--
            depth++
            level[depth] = indent
            name[depth] = clean(substr(rest, 1, colon - 1))
            if (depth == 4 && name[1] == "updates" && name[2] == "installs" && name[3] == id \
                && name[4] == want) {
                value = clean(substr(rest, colon + 1))
                if (value == "") {
                    collecting = 1
                    keyindent = indent
                    items = ""
                    next
                }
                if (substr(value, 1, 1) == "[" && substr(value, length(value), 1) == "]") {
                    value = flow(value)
                }
                print value
                done = 1
                exit
            }
        }
        END { if (collecting && !done) print items }' "$config"
}

ins_saved_channel() {
    ins_install_setting "$1" channel | grep -E '^(stable|canary|main)$' || :
}

# ins_effective_channel ORIGIN_URL SAVED: an explicit choice, else a pin follows its branch, else
# the saved choice, else stable for our repository's main line and main for anything else.
ins_effective_channel() {
    if [ -n "$IN_CHANNEL" ]; then
        printf '%s' "$IN_CHANNEL"
    elif [ -n "$IN_COMMIT" ]; then
        printf main
    elif [ -n "$2" ]; then
        printf '%s' "$2"
    elif [ "$IN_BRANCH" = main ] && ins_is_official "$1"; then
        printf stable
    else
        printf main
    fi
}

INS_STABLE_TAG='^v[0-9]{1,3}\.[0-9]+\.[0-9]+$'
INS_CANARY_TAG='^v[0-9]{1,3}\.[0-9]+\.[0-9]+-canary\.[0-9]{8}T[0-9]{6}Z$'

ins_valid_stable() { printf '%s\n' "$1" | grep -Eq "$INS_STABLE_TAG"; }

# The newest tag on stdin by SemVer order: X.Y.Z numerically, a canary below its own stable and
# canaries of one X.Y.Z by stamp. MODE stable keeps stable tags only; canary keeps both.
ins_newest_tag() {
    local pattern="$INS_STABLE_TAG"
    [ "$1" != canary ] || pattern="$INS_STABLE_TAG|$INS_CANARY_TAG"
    grep -E "$pattern" | awk -F'[v.-]' '{
        rank = ($5 == "canary") ? "1" $6 : "2"
        printf "%06d%06d%06d%s %s\n", $2, $3, $4, rank, $0
    }' | sort | tail -n 1 | cut -d' ' -f2
}

ins_remote_tags() {
    git ls-remote --tags --refs "$1" 'refs/tags/v*' 2>/dev/null | sed 's#.*refs/tags/##'
}

ins_api_latest() {
    curl -fsSL --connect-timeout 5 --max-time 20 -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/$INS_REPO_SLUG/releases/latest" 2>/dev/null |
        grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/'
}

ins_redirect_latest() {
    curl -fsSI --connect-timeout 5 --max-time 20 \
        "https://github.com/$INS_REPO_SLUG/releases/latest" 2>/dev/null |
        tr -d '\r' | sed -n 's#^[Ll]ocation:.*/releases/tag/\([^/]*\)$#\1#p' | tail -n 1
}

# ins_resolve_release CHANNEL ORIGIN_URL: the tag to install, or nothing when none answers.
# Stable: the GitHub API, then the releases/latest redirect (both for our repository only), then
# the remote's tags. Canary: the newest of the remote's canary and stable tags.
ins_resolve_release() {
    local tag=""
    if [ "$1" = stable ]; then
        if ins_is_official "$2"; then
            tag="$(ins_api_latest)"
            ins_valid_stable "$tag" || tag="$(ins_redirect_latest)"
        fi
        ins_valid_stable "$tag" || tag="$(ins_remote_tags "$2" | ins_newest_tag stable)"
        ins_valid_stable "$tag" || tag=""
    else
        tag="$(ins_remote_tags "$2" | ins_newest_tag canary)"
    fi
    printf '%s' "$tag"
}

# Park local edits before the tree moves: unmerged index entries are cleared first (a stash
# refuses them), then everything, untracked files included, goes into a labelled stash.
ins_park_local_changes() {
    local label="indus-install-autostash-$1"
    [ -n "$(git -C "$IN_DIR" status --porcelain 2>/dev/null)" ] || return 0
    if [ -n "$(git -C "$IN_DIR" ls-files --unmerged 2>/dev/null)" ]; then
        ins_warn "clearing unmerged index entries left by an earlier conflict"
        git -C "$IN_DIR" reset -q >/dev/null 2>&1 ||
            ins_fail "cannot clear the unmerged index in $IN_DIR" local_changes_blocked
    fi
    ins_run "Saving local changes" git -C "$IN_DIR" stash push --include-untracked -m "$label" ||
        ins_fail "could not save the local changes in $IN_DIR; commit or move them, then rerun" local_changes_blocked
    ins_warn "local changes saved in the stash '$label' (see: git -C \"$IN_DIR\" stash list)"
}

ins_repo_fresh() {
    local dir="$IN_DIR" url branch="$IN_BRANCH" channel tag="" target="" parent staged
    local attempt cloned=false no_checkout="" checked=false
    url="$(ins_repo_url)"
    if [ -e "$dir" ] || [ -L "$dir" ]; then
        if [ -d "$dir" ] && [ ! -L "$dir" ] && [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
            rmdir "$dir" || ins_fail "cannot take over the empty directory $dir" filesystem_error
        else
            ins_fail "$dir exists and is not an Indus Agent checkout; move it aside or pass --dir PATH" dir_not_checkout
        fi
    fi
    channel="$(ins_effective_channel "$url" "")"
    if [ "$channel" != main ]; then
        tag="$(ins_resolve_release "$channel" "$url")"
        [ -n "$tag" ] ||
            ins_fail "could not find the newest $channel release; rerun to try again, or pass --channel main to install the main branch" release_lookup_failed
        ins_info "Installing $tag (the $channel channel; --channel main follows every commit)"
    fi
    [ -z "$IN_COMMIT$tag" ] || no_checkout=--no-checkout
    parent="$(dirname "$dir")"
    mkdir -p "$parent" || ins_fail "cannot create $parent" filesystem_error
    staged="$(mktemp -d "$parent/.indus-clone-XXXXXX")" || ins_fail "cannot create a staging directory in $parent" filesystem_error
    INS_CLEANUP="$staged"
    for attempt in 1 2 3; do
        if ins_run --may-fail "Cloning $url ($branch), attempt $attempt of 3" \
            git clone --filter=blob:none ${no_checkout:+"$no_checkout"} --branch "$branch" "$url" "$staged/tree"; then
            cloned=true
            break
        fi
        rm -rf "$staged/tree"
        [ "$attempt" -eq 3 ] || ins_sleep "$((attempt * 5))"
    done
    if [ "$cloned" = false ]; then
        ins_warn "cloning failed 3 times; trying once more without checking out the files"
        if ins_run --may-fail "Cloning $url ($branch) without a checkout" \
            git clone --filter=blob:none --no-checkout --branch "$branch" "$url" "$staged/tree"; then
            cloned=deferred
        else
            rm -rf "$staged/tree"
            ins_fail "git clone failed; nothing was installed$(ins_cause)" git_clone_failed
        fi
    fi
    if [ -n "$IN_COMMIT" ]; then
        git -C "$staged/tree" merge-base --is-ancestor "$IN_COMMIT" "origin/$branch" 2>/dev/null ||
            ins_fail "commit $IN_COMMIT is not on branch $branch; nothing was installed" commit_not_on_branch
        target="$IN_COMMIT"
    elif [ -n "$tag" ]; then
        ins_run --may-fail "Fetching $tag" git -C "$staged/tree" fetch origin "+refs/tags/$tag:refs/tags/$tag" ||
            ins_fail "could not fetch $tag$(ins_cause); rerun, or pass --channel main" release_lookup_failed
        target="$tag"
    fi
    if [ -n "$target" ] || [ "$cloned" = deferred ]; then
        for attempt in 1 2; do
            if [ -n "$target" ]; then
                ins_run --may-fail "Checking out $target (attempt $attempt of 2)" \
                    git -C "$staged/tree" checkout --force --detach "$target" && checked=true
            else
                ins_run --may-fail "Checking out the files (attempt $attempt of 2)" \
                    git -C "$staged/tree" reset --hard HEAD && checked=true
            fi
            [ "$checked" = false ] || break
            [ "$attempt" -eq 2 ] || ins_sleep 5
        done
        [ "$checked" = true ] ||
            ins_fail "could not check out ${target:-the files}; nothing was installed$(ins_cause)" git_checkout_failed
    fi
    mv "$staged/tree" "$dir" || ins_fail "could not move the new checkout into $dir" filesystem_error
    rmdir "$staged" 2>/dev/null || :
    INS_CLEANUP=""
    ins_ok "cloned into $dir"
}

ins_repo_update() {
    local dir="$IN_DIR" url saved="" channel tag stamp ahead backup
    if [ -n "${INDUS_INSTALL_REPO_URL:-}" ]; then
        git -C "$dir" remote set-url origin "$INDUS_INSTALL_REPO_URL" ||
            ins_fail "cannot point origin at $INDUS_INSTALL_REPO_URL" git_fetch_failed
    fi
    url="$(git -C "$dir" config --get remote.origin.url 2>/dev/null)" || url=""
    [ -n "$IN_CHANNEL$IN_COMMIT" ] || saved="$(ins_saved_channel "$dir")"
    channel="$(ins_effective_channel "$url" "$saved")"
    stamp="$(ins_stamp)"
    if [ -n "$IN_COMMIT" ]; then
        ins_run "Fetching origin/$IN_BRANCH" git -C "$dir" fetch origin "+refs/heads/$IN_BRANCH:refs/remotes/origin/$IN_BRANCH" ||
            ins_fail "git fetch failed$(ins_cause)" git_fetch_failed
        git -C "$dir" merge-base --is-ancestor "$IN_COMMIT" "origin/$IN_BRANCH" 2>/dev/null ||
            ins_fail "commit $IN_COMMIT is not on branch $IN_BRANCH" commit_not_on_branch
        ins_park_local_changes "$stamp"
        ins_run "Checking out $IN_COMMIT" git -C "$dir" checkout --detach "$IN_COMMIT" ||
            ins_fail "git checkout failed$(ins_cause)" git_checkout_failed
        return 0
    fi
    if [ "$channel" != main ]; then
        tag="$(ins_resolve_release "$channel" "$url")"
        if [ -z "$tag" ] || ! ins_run --may-fail "Fetching $tag" git -C "$dir" fetch origin "+refs/tags/$tag:refs/tags/$tag"; then
            if [ -n "$IN_CHANNEL" ]; then
                ins_fail "could not reach the newest $channel release$(ins_cause); rerun, or pass --channel main" release_lookup_failed
            fi
            ins_warn "could not reach the newest $channel release; keeping $dir as it is"
            return 0
        fi
        # A release only moves a checkout forward; any other move is `indus update`'s decision.
        if git -C "$dir" merge-base --is-ancestor "$tag" HEAD 2>/dev/null; then
            ins_info "$dir already has $tag"
            return 0
        fi
        if ! git -C "$dir" merge-base --is-ancestor HEAD "$tag" 2>/dev/null; then
            ins_info "$tag does not follow this checkout's commit; leaving it for 'indus update'"
            return 0
        fi
        ins_park_local_changes "$stamp"
        ins_run "Checking out $tag" git -C "$dir" checkout --detach "$tag" ||
            ins_fail "git checkout failed$(ins_cause)" git_checkout_failed
        ins_ok "moved $dir to $tag"
        return 0
    fi
    ins_run "Fetching origin/$IN_BRANCH" git -C "$dir" fetch origin "+refs/heads/$IN_BRANCH:refs/remotes/origin/$IN_BRANCH" ||
        ins_fail "git fetch failed$(ins_cause)" git_fetch_failed
    ins_park_local_changes "$stamp"
    if git -C "$dir" show-ref --verify --quiet "refs/heads/$IN_BRANCH"; then
        ins_run "Checking out $IN_BRANCH" git -C "$dir" checkout "$IN_BRANCH" ||
            ins_fail "git checkout failed$(ins_cause)" git_checkout_failed
    else
        ins_run "Checking out $IN_BRANCH" git -C "$dir" checkout -b "$IN_BRANCH" "origin/$IN_BRANCH" ||
            ins_fail "git checkout failed$(ins_cause)" git_checkout_failed
    fi
    if ins_run --may-fail "Fast-forwarding to origin/$IN_BRANCH" git -C "$dir" merge --ff-only "origin/$IN_BRANCH"; then
        ins_ok "$dir is at origin/$IN_BRANCH"
        return 0
    fi
    # Not a fast-forward: save every commit the reset would drop, and only then reset.
    ahead="$(git -C "$dir" rev-list --count "origin/$IN_BRANCH..HEAD" 2>/dev/null)" ||
        ins_fail "cannot count the local commits; refusing to reset $dir" git_reset_failed
    if [ "$ahead" -gt 0 ]; then
        backup="refs/indus/install-backups/$stamp"
        git -C "$dir" update-ref "$backup" HEAD ||
            ins_fail "cannot save $ahead local commit(s); refusing to reset $dir" git_reset_failed
        ins_warn "$ahead commit(s) not on origin/$IN_BRANCH saved as $backup (git -C \"$dir\" log $backup)"
    fi
    ins_run "Resetting to origin/$IN_BRANCH" git -C "$dir" reset --hard "origin/$IN_BRANCH" ||
        ins_fail "git reset failed$(ins_cause)" git_reset_failed
    ins_warn "$dir could not fast-forward; it now matches origin/$IN_BRANCH"
}

ins_stage_repository() {
    local aside
    if [ -e "$IN_DIR/.git" ] && ! git -C "$IN_DIR" rev-parse --verify -q HEAD >/dev/null 2>&1; then
        aside="$IN_DIR.broken-$(ins_stamp)"
        ins_warn "$IN_DIR holds an interrupted clone (no commits); moving it aside to $aside"
        mv "$IN_DIR" "$aside" || ins_fail "cannot move $IN_DIR aside" filesystem_error
    fi
    if [ -e "$IN_DIR/.git" ]; then
        ins_repo_update
    else
        ins_repo_fresh
    fi
}
