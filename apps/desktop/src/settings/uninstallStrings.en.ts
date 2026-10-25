// English strings of Settings → About → Uninstall (P24-34).

export const UNINSTALL_STRINGS = {
  "settings.uninstall.title": "Uninstall",
  "settings.uninstall.heading": "Uninstall Indus Agent",
  "settings.uninstall.intro":
    "Indus quits first; a small helper then runs `indus uninstall` and removes what you chose.",
  "settings.uninstall.checking": "Checking how Indus was installed",
  "settings.uninstall.level.gui": "Remove the app only",
  "settings.uninstall.level.gui.desc": "The desktop app goes; the agent and all your data stay.",
  "settings.uninstall.level.keep-data": "Remove the app and the runtime, keep my data",
  "settings.uninstall.level.keep-data.desc":
    "Settings, sessions, memories and keys stay in your home folder for a later reinstall.",
  "settings.uninstall.level.full": "Remove everything",
  "settings.uninstall.level.full.desc":
    "The app, the runtime and this home's data and keys. This cannot be undone.",
  "settings.uninstall.typeToConfirm": "Type {phrase} to confirm",
  "settings.uninstall.confirm": "Quit and uninstall",
  "settings.uninstall.cancel": "Cancel",
  "settings.uninstall.ask": "Indus will quit now and then {what}.",
  "settings.uninstall.failed": "Uninstall did not start: {message}",
} as const satisfies Record<string, string>;
