# Uninstall levels (`indus uninstall`, P23-20)

The flags and what each level removes. P24-34 (uninstall from the desktop's Settings) runs these
command lines from its helper after the app exits; changing a row here means updating P24-34.
Code: `src/indus/cli/uninstall.py`; tests: `tests/unit/cli/test_uninstall.py`.

## Flags

| Flag | Meaning |
|---|---|
| (none) | Interactive: shows the install, asks 1 keep data (default) / 2 full / 3 cancel, then a typed `yes`. Without a terminal: prints how to run it, exit 1. |
| `-y`, `--yes` | No questions. Keep data unless `--full`. Named profiles are never removed this way. |
| `--full` | Level **full** (interactive: skips the menu, still asks about named profiles and for `yes`). |
| `--gui` | Level **gui**: the desktop app only. |
| `--data` | Level **data**: this home's data only. The one level allowed when another manager owns the install. |
| `--gui-summary` | Prints what the desktop app installed as JSON and exits 0; changes nothing. Not combinable. |
| `--dry-run` | Prints the plan of the chosen level and exits 0; changes nothing. |

`--full`, `--gui` and `--data` exclude each other (exit 2).

## Levels

| What | keep-data | full | gui | data |
|---|---|---|---|---|
| Gateway service of this home (systemd user unit / launchd agent; a running one is stopped first) | removed | removed | kept | kept (refused while it runs) |
| Manually started gateway of this home | SIGTERM to its recorded pid after the planned-stop record | same | kept | kept (refused while it runs) |
| Marked PATH block (`# >>> indus >>>` … `# <<< indus <<<`) in `.bashrc`, `.bash_profile`, `.bash_login`, `.profile`, `.zshrc`, `.zprofile` (`ZDOTDIR`), fish `config.fish` | removed | removed | kept | kept |
| Windows user PATH entry `<root>\bin` | removed | removed | kept | kept |
| Launcher `~/.local/bin/indus`, Windows `<root>\bin\indus.cmd` (only when it carries the installers' launcher comment and names the checkout, or is a link into it) | removed | removed | kept | kept |
| Start menu entry `Indus.lnk` (only when it names the checkout) | removed | removed | removed | kept |
| Code checkout | removed | removed | kept (its `apps/desktop/release` removed) | kept |
| Installer tools `<root>/tools` (uv, portable Git) | removed | removed | kept | kept |
| Keychain entries of this home | kept | removed | kept | removed |
| Desktop app user data (Electron userData, `Indus`) | kept | removed | removed | kept |
| This home | kept | removed (default profile with named profiles kept: everything but `profiles/`) | kept | emptied except `profiles`, `tools`, `bin`, `.indus-bootstrap-complete` and the checkout |
| Named profiles (from the default profile) | kept | interactive "y": their services, keys, alias commands and folders removed; `-y`: kept | kept | kept |

## Refusals (exit 1)

| Case | Levels refused | Message points at |
|---|---|---|
| Install kind `nix`, `docker` or `desktop` (`indus --version` "Install method") | keep-data, full, gui | the owning manager, and `indus uninstall --data` |
| Code tree without `.git` (not installed by our installer) | keep-data, full, gui | removing it the way it was installed, and `--data` |
| Home or root inside the code dir; code dir is a filesystem root or the user's home | keep-data, full | moving the home out first |
| `--data` while this profile's gateway runs | data | `indus gateway stop` |

Exit codes: 0 done, dry run, summary or cancelled; 1 refused, no terminal without `-y`, or a step
failed (each failure is printed; a file left alone on purpose is a note, not a failure); 2 usage.
Windows has no native gateway service in 1.0 (D-16.55), so there is no scheduled task to remove.
