# Desktop app

The desktop app is a window onto your Indus Agent: conversations in a sidebar, the chat in the
middle and tool panes around it. It runs on macOS, Windows and Linux.

## First start

When the app opens it looks for an Indus Agent it can use:

1. a gateway daemon already running for your profile on this computer;
2. an installed `indus` it can start;
3. otherwise it offers to install one. The install runs the same steps as the install script and
   shows each one as it goes; nothing is installed without your click.

You can also connect to a backend on another machine (Settings, Gateways). When no model provider is
set up yet, an onboarding screen asks for one: sign in with a device code, paste an API key, point
it at a local model server, or enter any OpenAI-compatible endpoint. The endpoint is tested before
anything is saved. "Choose later" closes the screen.

## Chatting

The chat works like the other surfaces: replies stream in, tool calls show as rows you can expand,
approvals and questions appear as cards, and a status bar shows the model and context use. Slash
commands work here too. Conversations started in the app are the same sessions you see in the
terminal and the dashboard.

## Panes

Around the chat you can open:

- **Preview**: a local web page (for example the dev server of the project you are working on). The
  page runs in an isolated view: it cannot reach the app, open windows or navigate it elsewhere.
- **Files**: browse the session's folder; only folders the app has opened for a session can be
  reached, also through links.
- **Review**: the git changes the agent made, file by file.
- **Terminal**: a shell in the session's folder.

Panes can be dragged into tabs and splits; the layout presets (Basic, Focus, Default, Terminal deck,
Quad) rearrange them at once. The Simple interface mode hides the technical panes and keeps the
chat first.

## Around the desktop

- **Pop out** puts one conversation in a small window that floats above other apps.
  `Ctrl+Shift+H` (`Cmd+Shift+H` on macOS) shows or hides it from anywhere.
- **Tray**: optionally, minimising or closing the main window hides it in the system tray.
- **Keep awake**: stop the computer sleeping while the agent works (default), always, or never.
- **Notifications** when a reply finishes or needs an answer while the window is in the background.
- **Links**: `indus://` links open a page of the app, or offer to install a skill or add an MCP
  server; installing always asks first.

## Updates

macOS and Linux (AppImage) builds check for updates in the background and offer to restart into
the new version; the channel (stable or canary) is chosen in Settings. Builds without an updater
show where to get the new version instead. A source install is updated with `indus update`.

## Security

The app's window runs with a strict content security policy, and only the app's own code can talk
to its main process. Previews and other web content get no access to the app, and links in them
open in your browser only after a check. Settings, Diagnostics collects logs for a bug report with
secrets removed.
