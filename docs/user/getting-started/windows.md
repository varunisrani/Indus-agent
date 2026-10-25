# Windows and WSL2

On Windows, Indus Agent works best split in two: the desktop app runs natively, and the agent's
command line, terminal UI and messaging gateway run inside WSL2 (a real Linux next to Windows).

| Part | Native Windows | WSL2 |
|---|---|---|
| Desktop app | supported | — |
| PowerShell installer | supported | — |
| Classic CLI and terminal UI | works, best effort | supported |
| Gateway daemon (the desktop app's backend) | works, best effort | supported |
| Messaging gateway | — | supported |
| Docker image | through Docker Desktop | through Docker Desktop or Docker in WSL2 |

## Setting up WSL2

1. In an administrator PowerShell, run `wsl --install` and restart when asked. This installs
   Ubuntu by default.
2. Open the Ubuntu terminal and run the Linux install script from [Install](install.md).
3. Run `indus setup`, then `indus`.

Your Windows drives are visible inside WSL2 under `/mnt/c`, `/mnt/d` and so on, but projects are
much faster when they live in the Linux file system (your WSL home folder).

## Native Windows

The PowerShell installer (`install.ps1`) sets up Indus Agent for the current user without
administrator rights. It also downloads a portable copy of Git for Windows, checked against a
pinned digest, because the agent runs shell commands through Git Bash on native Windows. This
terminal backend is best effort: most commands behave as on Linux, but tools that expect a POSIX
system can differ. When something does not work natively, use WSL2.

When the installer reaches the messaging gateway step on Windows it points you to WSL2: the gateway
is a long-running service and is supported there.

### The gateway daemon (best effort)

The desktop app talks to the agent through its gateway daemon, and that daemon also runs natively.
`indus gateway ensure`, `start`, `stop` and `status` work as on Linux; the desktop app starts the
daemon by itself when it opens. What differs:

- **Control channel.** Instead of a Unix socket the daemon listens on a named pipe with a fresh
  random name (`\\.\pipe\indus-gw-…`), written into `gateway.sock.path` in your Indus home.
  Only processes running as your Windows user may use its owner-only requests.
- **Stopping.** Windows has no signals to catch, so `indus gateway stop` asks the daemon through
  that pipe and waits; `--force` ends it at once.
- **No service.** There is no Windows service; a daemon the desktop app started stops itself after
  ten idle minutes (`gateway.idle_exit_seconds`).
- **First run.** With no model provider set up yet the daemon still starts, and the desktop app
  opens its provider setup; what you choose there is used from your next message on, without a
  restart. `indus setup model` does the same from a terminal.

Messaging platforms (Telegram, WhatsApp and the others) remain supported in WSL2 only.

### Shell commands through Git Bash (best effort)

When Indus Agent runs natively on Windows, the `local` terminal backend hands every shell command
to Git Bash. Nothing needs configuring when Git for Windows is installed; the agent picks the first
`bash.exe` it finds in this order:

1. the file named by the `INDUS_GIT_BASH` environment variable (if that file is missing, the
   backend reports it instead of guessing);
2. the portable Git the PowerShell installer placed under `<home>\tools`, the newest version first;
3. the Git installation that the `git.exe` on your `PATH` belongs to;
4. the usual install folders: `Program Files\Git`, `Program Files (x86)\Git`, the per-user
   `AppData\Local\Programs\Git`, and Scoop's `git` app;
5. any other `bash.exe` on your `PATH`.

The `bash.exe` in `System32` and the Store alias in `WindowsApps` start WSL instead and are never
used. MinGit has no Bash and does not count either.

What to expect:

- **Paths.** The agent's tools keep Windows paths such as `C:\Users\you\project`; commands see the
  same folder as `/c/Users/you/project`. A `cd` inside a command carries over to the next command,
  as on Linux.
- **Arguments to Windows programs** are passed as typed: Git Bash's automatic path rewriting is off
  (`MSYS_NO_PATHCONV=1` and `MSYS2_ARG_CONV_EXCL=*`, unless you set them yourself), so options like
  `/F` reach programs such as `tasklist` unchanged. Give Windows programs Windows paths.
- **Stopping.** A command that runs too long or that you interrupt is stopped together with every
  program it started. Programs a command started in the background (a script that runs
  `some-server &`, say) keep running after that command ends normally.
- **Foreground only.** The agent runs commands in the foreground: a command that ends in `&` is
  refused with a hint to send it without the `&`, and a timeout above the 600-second foreground
  limit is cut to that limit (the result says so). Servers, watchers and longer jobs need WSL2 or a
  Docker or SSH terminal backend.
- **Text.** Commands get a UTF-8 console and `PYTHONUTF8=1`, so accented and non-Latin output comes
  through intact. Temporary files live under `<home>\cache\terminal`.
- **Not available natively:** `sudo`, and the background-process, scheduled-job and code-execution
  helpers that need a POSIX system. Use WSL2 for those.

If a command misbehaves only on native Windows, try it in WSL2; that is the supported setup.

## The desktop app with WSL2

The desktop app installs and looks after its own copy of Indus Agent on Windows. It can also
connect to a backend that runs somewhere else, which you add in its settings under Gateways. See
[Desktop app](../user-guide/desktop.md).

## Docker Desktop

Docker Desktop has no host networking, so the dashboard in the container cannot listen on your
machine's loopback address directly and needs a sign-in. The [Docker](../user-guide/docker.md)
page shows how to set one up before starting the Windows compose file.
