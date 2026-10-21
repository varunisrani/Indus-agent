# Install

There are four ways to install Indus Agent. They all end with the same `indus` command and the same
home folder (`~/.indus`), so you can switch later without losing anything.

| Channel | Good for | Updates with |
|---|---|---|
| Install script (Linux, macOS) or PowerShell script (Windows) | most people | `indus update` |
| Docker image | servers, always-on messaging | pulling a new image |
| Nix flake | NixOS and Nix users | `nix flake update` |
| Desktop app | people who prefer a window | the app itself |

Indus Agent is not published to PyPI: installing it with `pip install` from a package index is not
supported.

## Install script (Linux and macOS)

```bash
curl -fsSL https://github.com/indusagi/indus-agent/releases/latest/download/install.sh | bash
```

The script downloads a pinned copy of [uv](https://docs.astral.sh/uv/) (checked against a SHA-256
digest), gets a managed Python, checks out the release into `~/.indus/indus-agent`, installs the
Python packages exactly as locked, puts `indus` on your `PATH` and then offers to run
[`indus setup`](quickstart.md). Every download is checked against a digest pinned in the release; a
mismatch stops the install.

Useful options (run `bash install.sh --help` for all of them):

| Option | What it does |
|---|---|
| `--channel stable\|canary\|main` | which line to follow; `stable` is the default |
| `--dir PATH` | where the code goes (default `~/.indus/indus-agent`) |
| `--indus-home PATH` | another home folder |
| `--non-interactive` | ask nothing; skip the setup questions |
| `--skip-browser` | leave out the browser tools (Chromium is large) |
| `--include-desktop` | also set up the desktop app |

To pass options through the pipe: `curl -fsSL <url> | bash -s -- --channel canary`.

## PowerShell script (Windows)

```powershell
irm https://github.com/indusagi/indus-agent/releases/latest/download/install.ps1 | iex
```

The same steps as the shell script, with the same options in PowerShell spelling (`-Channel`,
`-IndusHome`, `-NonInteractive`). On Windows the command-line agent is best run inside WSL2; see
[Windows and WSL2](windows.md) for what works where.

## Docker

```bash
docker pull ghcr.io/indusagi/indus-agent:stable
```

The image runs the messaging gateway and the dashboard; the [Docker](../user-guide/docker.md) page
has the compose files and the steps for Linux and Windows.

## Nix

The repository is a flake. Its packages are `default`, `minimal`, `messaging`, `tui` and `web`:

```bash
nix run github:indusagi/indus-agent -- --version
nix profile install github:indusagi/indus-agent
```

NixOS and Home Manager modules are included. Updates come from Nix (`nix flake update` and a
rebuild); `indus update` refuses to touch a Nix install.

## Desktop app

The desktop app ships installers for macOS (DMG), Windows (NSIS) and Linux (AppImage). On first
start it looks for Indus Agent on your computer and, when there is none, offers to install it with
the same steps as the install script, showing each one as it runs. See [Desktop app](../user-guide/desktop.md).

## What gets installed

The standard install includes the extras most people need: web search, document reading,
pseudo-terminals, the dashboard, editor support (ACP), every messaging platform that installs
from wheels, and (unless `--skip-browser`) the browser tools. Other optional parts are extras you
add later, for example `voice` (local speech to text), `research` (dataset tools), `modal`,
`daytona` and `vercel` (cloud sandboxes) or `matrix-e2ee` (encrypted Matrix rooms, Linux only).

To add an extra to an install made by a script, add its name to the install's saved list and
update:

```bash
indus update --install-id                     # prints this install's id
indus config set updates.installs.<id>.extras "[web, docs, pty, dashboard, acp, messaging, browser, voice]" --force
indus update
```

## Check the install

<!-- test -->
```console
$ indus --version
Indus Agent ...
...
```

`indus --version` also says how Indus Agent was installed and where. `indus doctor` checks the rest
of the setup and points at anything that needs fixing.
