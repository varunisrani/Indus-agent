// English strings of the composer's menus (W52-h): the completion drawer, the `?` help card, the
// attach menu and its dialogs, the model and effort menus. Spread into `src/i18n/en.ts`.

export const COMPOSER_MENU_STRINGS = {
  // completion drawer
  "drawer.group.session": "Session",
  "drawer.group.config": "Config",
  "drawer.group.tools": "Tools",
  "drawer.group.info": "Info",
  "drawer.group.exit": "Exit",
  "drawer.group.other": "Other",
  "drawer.scope.file": "File",
  "drawer.scope.folder": "Folder",
  "drawer.empty": "No matches",
  "drawer.emptyMention": "Try @file: or @folder:",
  "drawer.emptySlash": "Try /help",
  "drawer.loading": "Looking…",

  // `?` help card
  "help.label": "Composer help",
  "help.commands": "Common commands",
  "help.hotkeys": "Hotkeys",
  "help.mention": "files, folders, urls, git",
  "help.slash": "commands",
  "help.self": "this help",
  "help.send": "send · new line",
  "help.history": "earlier prompts",
  "help.escape": "close menus",
  "help.focus": "focus the composer",
  "help.footer": "shows every command",

  // attach menu
  "attach.menu": "Attach",
  "attach.files": "Files…",
  "attach.folder": "Folder…",
  "attach.images": "Images…",
  "attach.paste": "Paste image",
  "attach.url": "URL…",
  "attach.snippets": "Prompt snippets…",
  "attach.tipBefore": "Tip: type",
  "attach.tipAfter": "to point at files inline.",
  "attach.urlTitle": "Add a web page",
  "attach.urlField": "Address",
  "attach.urlHint": "The page is fetched and read with your message.",
  "attach.urlInvalid": "Use an http or https address.",
  "attach.urlAdd": "Add",
  "attach.cancel": "Cancel",
  "attach.snippetsTitle": "Prompt snippets",
  "attach.snippetsHint": "Start from a ready prompt; edit it before sending.",
  "attach.snippet.review": "Review this change",
  "attach.snippet.reviewHint": "Look for bugs and risky edits in the working tree",
  "attach.snippet.reviewText":
    "Review the uncommitted changes in @diff. List real bugs first, then risky spots, then small cleanups.",
  "attach.snippet.plan": "Plan the work",
  "attach.snippet.planHint": "Break a task into steps before touching code",
  "attach.snippet.planText":
    "Before changing anything, write a short step-by-step plan for this task and wait for my go: ",
  "attach.snippet.explain": "Explain this code",
  "attach.snippet.explainHint": "Walk through a file or folder in plain words",
  "attach.snippet.explainText": "Explain how this works, in plain words, file by file: @folder:",

  // model menu
  "model.loading": "Model not known yet",
  "model.favourites": "Favorites",
  "model.optionsFor": "Options for {model}",
  "model.refresh": "Refresh models",
  "model.addCustom": "Add custom model…",
  "model.customPlaceholder": "Type a model id",
  "model.custom": "Custom model id",
  "model.edit": "Edit models…",
  "model.signIn": "Sign in",
  "model.back": "Back to models",

  // effort menu
  "effort.options": "Options",
  "effort.thinking": "Thinking",
  "effort.effort": "Effort",
  "effort.max": "Max",
  "effort.maxLong": "Max",
  "effort.ultra": "Ultra",
  "effort.ultraLong": "Ultra",
} as const;
