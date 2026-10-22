# Rollback sheet (P25-23)

Prepared by the agents, run by the owner. Agents never move a release, an image alias or the
channel record (skill reference 08 §1, §9). Use this sheet when the post-release smoke
(`scripts/release/smoke/`) or the first users find a release that must not stay on offer.

Placeholders, filled in at the time:

| Placeholder | Meaning | Example |
|---|---|---|
| `<repo>` | the release base, `RELEASE_REPOSITORY` in `src/indus/foundation/versions.py` (owner decision D-16.690; today `indusagi/indus-agent`) | `indusagi/indus-agent` |
| `<bad>` | the version being taken off offer | `1.0.0` |
| `<good>` | the previous version on the same channel (what the levers point back to) | `0.9.3` |
| `<next>` | the fix-forward version, the patch after `<bad>` | `1.0.1` |
| `<channel>` | `stable` or `canary` | `stable` |

## The rule: switch off, never delete, fix forward

- **Never** delete, move or re-point a tag: not the receipt `v<bad>`, not the attempt ref
  `rc.<N>-v<bad>`, not an `abandoned-…` marker. A receipt is never replaced (`publish.py`
  refuses), and every install that saw it keeps seeing it.
- **Never** reuse a version number, delete the GitHub Release (back to draft only), or delete an
  image digest (aliases move; digests stay).
- `release.py abandon` is for attempts that were never published. It is not a rollback tool.
- The only way to move every install off `<bad>` is a newer version: **fix forward with `<next>`**.
  The switches below only stop new people from getting `<bad>` while `<next>` is prepared.

## Who is affected, and which switch stops it

| Who | How they find a release | Switch | After the switch |
|---|---|---|---|
| New script installs (`install.sh`, `install.ps1`, `stable`) | the releases API's "latest", then the `releases/latest` redirect, then the remote's tags | 1. release back to draft | the API and the redirect name `<good>`; **the tag fallback still finds `v<bad>`** when both fail (rare) |
| New script installs (`canary`) | the remote's newest canary or stable tag | none | they get `<bad>` until `<next>` is tagged |
| Existing source installs (`indus update`) | tags fetched from the checkout's `origin`; never the release page | none | **`<bad>` stays on offer** until `<next>` exists; a person can go back by hand (below) |
| Desktop app | the releases API (drafts are not listed) | 1. release back to draft | not offered any more; apps already on `<bad>` stay until `<next>` |
| Docker users | the aliases `<channel>`, `latest` and their variants | 2. aliases back to `<good>` | a new pull gets `<good>`; running containers keep their image |
| Nix users | a flake ref they chose (`nix flake update`) | none | fix forward |
| The channel record (`channels.json` on the `channels` release) | nothing reads it in 1.0 yet (written by the publish controller) | 3. edit by hand | names `<good>` again; the controller refuses backward moves, hence by hand |

## Steps, in order

Log each command and its output in `docs/internals/rehearsal-log.md` (or the release checklist).

### 1. The release back to draft

```bash
gh release edit "v<bad>" --repo "<repo>" --draft=true
gh release view "v<bad>" --repo "<repo>" --json isDraft,tagName      # isDraft: true
curl -fsSI "https://github.com/<repo>/releases/latest" | grep -i '^location:'   # …/tag/v<good>
```

The tag `v<bad>` stays. The downloads of a draft stop working for everyone who is not a
maintainer, so anything that linked them breaks until `<next>` is out.

### 2. Image aliases back to `<good>`

The digests of `<good>` are in its receipt tag (a JSON record with `images`):

```bash
git fetch origin "refs/tags/v<good>:refs/tags/v<good>"
git tag --list "v<good>" --format='%(contents)'      # {"images": {"product": "sha256:…", …}, …}
```

The aliases the publish controller moves (`scripts/release/claims.py` `ALIASES` and `IMAGES`):

| Image | stable aliases | canary aliases |
|---|---|---|
| product `ghcr.io/<repo>` | `stable`, `latest` | `canary` |
| product with the browser `ghcr.io/<repo>` | `stable-browser`, `latest-browser` | `canary-browser` |
| sandbox `ghcr.io/<repo>-sandbox` | `stable-slim`, `latest-slim` | `canary-slim` |
| sandbox with the browser `ghcr.io/<repo>-sandbox` | `stable-browser`, `latest-browser` | `canary-browser` |

For each row and each alias (log in to GHCR first, `docker login ghcr.io`):

```bash
docker buildx imagetools create --tag "ghcr.io/<repo>:stable" "ghcr.io/<repo>@<product digest of <good>>"
docker buildx imagetools inspect "ghcr.io/<repo>:stable"      # Digest: <product digest of <good>>
```

### 3. The channel record by hand

`update_channel_record` refuses to move a channel backwards, so the controller cannot do this.

```bash
gh release download channels --repo "<repo>" --pattern channels.json --dir /tmp/channels --clobber
# edit /tmp/channels/channels.json: set "<channel>" to <good>'s entry
#   {"version": "<good>", "tag": "v<good>", "commit": "<good commit>",
#    "images": {<good's images from its receipt>}, "published_at": "<good's time>"}
gh release upload channels /tmp/channels/channels.json --repo "<repo>" --clobber
```

When `<next>` is published, the controller moves the record forward from `<good>` as usual.

### 4. Tell people

- A short notice on the `<good>` release and in the issue tracker: what is wrong with `<bad>`,
  that `<next>` is coming, and what to do meanwhile.
- Source installs that already updated (`indus update` keeps offering `<bad>` until `<next>`):
  take a full backup first (`indus backup`), then go back by hand:

  ```bash
  git -C ~/.indus/indus-agent checkout --detach "v<good>"
  uv sync --directory ~/.indus/indus-agent --frozen --no-dev   # plus --extra <name> for each saved extra
  indus --version
  ```

  `indus update` refuses a target whose store schema is older than the live store; when `<bad>`
  raised the schema, going back needs the data from before the update: the snapshot
  `indus update` took (`<home>/state-snapshots/…-pre-update`) or a full backup restored with
  `indus import`.
- Desktop users on `<bad>`: wait for `<next>`, or install `<good>` from its release page over it.
- Docker users: `docker pull ghcr.io/<repo>:stable` and recreate the container.

### 5. Fix forward with `<next>`

```bash
uv run python scripts/release/release.py --commit <fixed sha on main> --bump patch --dry-run
uv run python scripts/release/release.py --commit <fixed sha on main> --bump patch
```

The release workflow gates, builds, publishes `<next>`, moves the aliases and the channel record
forward and makes the release public last. Then run the smoke on each OS:

```bash
bash scripts/release/smoke/linux.sh --tag v<next>
bash scripts/release/smoke/macos.sh --tag v<next>
powershell -ExecutionPolicy Bypass -File scripts/release/smoke/windows.ps1 -Tag v<next>
```

### 6. Afterwards

Leave `v<bad>` as a draft (do not publish it again). Record what happened, the commands and the
times in the release checklist; add the cause to the triage list.

## A canary

The same steps with `<channel>` = `canary`: the release is a prerelease (back to draft the same
way), the alias is `canary` (and its variants), the record's `canary` entry. Canary users move on
with the next canary, which `canary.yml` cuts from main every day.

## The post-release smoke

`scripts/release/smoke/linux.sh`, `macos.sh` and `windows.ps1` (P25-23) check a published release
from its public downloads on a clean machine. Each takes `--tag` (`-Tag`), prints one
`<step> PASS|FAIL|SKIP <detail>` line per step and exits 1 when a step failed; `--dry-run`
(`-DryRun`) prints the plan and the URLs without fetching anything; `--interactive`
(`-Interactive`) asks a person to confirm the approval and the desktop chat window.

| Step | What it proves |
|---|---|
| `install` | the installer is attached to the release and installs exactly that tag |
| `version` | `indus --version` works; `indus update --check` says the install is on that release |
| `setup` | `indus setup --non-interactive` runs; the model points at the fake endpoint (the release's own scripted model server) |
| `turn` | one turn through the gateway daemon |
| `approval` | a risky command with nobody to approve it does not run |
| `resume` | the first session resumes by id |
| `doctor` | `indus doctor` passes |
| `dashboard-gate` | the dashboard serves on loopback; its settings API refuses a request without the token |
| `dashboard-login` | the dashboard page carries the loopback session token and the API accepts it |
| `desktop-download` | the desktop package of this OS is on the release (Linux AppImage unpacked, macOS DMG mounted, Windows installed per user) |
| `licences` | `THIRD_PARTY_NOTICES.md` is in the checkout and inside the desktop package |
| `signature` | macOS: `codesign` and Gatekeeper accept the app; Windows: Authenticode is valid |
| `desktop-launch` | the desktop app starts and reaches the chat (a person confirms) |
| `uninstall` | `indus uninstall -y` removes code and launcher and keeps the data |
