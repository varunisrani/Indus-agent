// English strings of the ambient surfaces (P20-35..37): HUD, quick entry, notifications, keep
// awake and update / skew notices. Spread into `src/i18n/en.ts`.

export const AMBIENT_STRINGS = {
  "hud.label": "Floating chat",
  "hud.grip": "Hold to move",
  "hud.reset": "Reset position",
  "hud.close": "Close floating chat",
  "hud.empty": "Open a chat in Indus and pop it out to follow it here.",
  "hud.resize": "Drag to resize",

  "quick.label": "Quick message",
  "quick.placeholder": "Ask Indus",
  "quick.send": "Send",
  "quick.hint": "Enter to send, Esc to close",
  "quick.sending": "Sending",
  "quick.error.empty": "Type a message first.",
  "quick.error.noTarget": "Open an Indus window first. Your text is kept.",
  "quick.error.unconfirmed":
    "Indus has not confirmed the message yet. It may still arrive, so check the chat before sending it again.",
  "quick.error.failed": "Indus could not send that message.",
  "quick.error.busy": "The previous message is still on its way.",

  "notify.turnDone.title": "Reply ready",
  "notify.turnDone.body": "Indus finished working on your chat.",
  "notify.turnFailed.title": "Indus stopped",
  "notify.ask.title": "Indus needs your answer",
  "notify.ask.approval": "A command is waiting for your approval.",
  "notify.ask.other": "A question is waiting for you.",
  "notify.open": "Open",

  "keepAwake.offer":
    "Your computer went to sleep while Indus was working. Keep it awake while a reply is running?",
  "keepAwake.accept": "Keep awake while working",
  "keepAwake.dismiss": "Not now",

  "update.available": "Indus {version} is available.",
  "update.manualCommand": "Indus {version} is available. Run {command} to update.",
  "update.install": "Update and restart",
  "update.download": "Open download page",
  "update.later": "Later",
  "update.downloading": "Downloading the update ({progress}%)",
  "update.restarting": "Restarting to install the update",
  "update.failed": "The update did not finish: {error}",

  "skew.mixed":
    "This app has files from two different builds. Reinstall or update Indus to fix it.",
  "skew.behind": "The Indus runtime is newer than this app. Update the app to match.",
  "skew.stale":
    "Part of this window could not load because Indus changed underneath it. Reload the window, or update the app if it happens again.",
  "skew.reload": "Reload window",
  "skew.check": "Check for updates",
} as const;
