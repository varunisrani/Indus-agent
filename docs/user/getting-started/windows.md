# Windows and WSL2

On Windows, Indus Agent works best split in two: the desktop app runs natively, and the agent's
command line, terminal UI and messaging gateway run inside WSL2 (a real Linux next to Windows).

| Part | Native Windows | WSL2 |
|---|---|---|
| Desktop app | supported | — |
| PowerShell installer | supported | — |
| Classic CLI and terminal UI | works, best effort | supported |
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

## The desktop app with WSL2

The desktop app installs and looks after its own copy of Indus Agent on Windows. It can also
connect to a backend that runs somewhere else, which you add in its settings under Gateways. See
[Desktop app](../user-guide/desktop.md).

## Docker Desktop

Docker Desktop has no host networking, so the dashboard in the container cannot listen on your
machine's loopback address directly and needs a sign-in. The [Docker](../user-guide/docker.md)
page shows how to set one up before starting the Windows compose file.
