# Updating and removing

## Updating a script install

```bash
indus update --check     # exit code 10 when a newer release is waiting, 0 otherwise
indus update             # update to the newest release of this install's channel
```

An install follows one channel:

- `stable` (the default): the newest released version;
- `canary`: the newest pre-release, or a newer stable release when there is one, so a canary
  install is never left behind;
- `main`: the tip of the development branch.

`indus update --set-channel canary` saves another channel for this install; `--channel` uses one
for a single run. Without an explicit choice an install never moves backwards.

An update runs as one transaction: it snapshots this profile (`updates.pre_update_backup`: `quick`
by default, `full` or `off`), moves the code to the new release, checks that the new code compiles,
builds a fresh Python environment beside the old one and proves it starts, swaps the two, and
finally restarts this profile's messaging gateway on the new code. If any step fails, or you press
Ctrl+C before the swap, the old code and environment are put back. `indus update --plan` prints
what would happen without doing it.

`indus --version` mentions a waiting update; turn those notices off with
`indus config set updates.check false`.

## Other installs

| Install | How to update |
|---|---|
| Docker | pull the new image and recreate the containers (`docker compose pull && docker compose up -d`) |
| Nix | `nix flake update` and rebuild; `indus update` refuses to run here |
| Desktop app | the app checks for updates itself and offers to restart into the new version |

## Removing Indus Agent

```bash
indus uninstall --dry-run   # show what would be removed
indus uninstall             # remove the program, keep your data
```

By default your home folder (settings, conversations, memories, skills) stays, so a later install
picks up where you left off. `--full` also removes the home folder, the keys stored in the
keychain and the desktop app's data; `--data` removes only the data; `--gui` removes only the
desktop app.

Before a big change, `indus backup` writes the whole home folder into a zip file and
`indus import <zip>` restores it.
