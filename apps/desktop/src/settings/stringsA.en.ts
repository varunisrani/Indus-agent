// English strings of the settings slice A pages (W52-i): Model, Chat, Appearance, Workspace,
// Safety, Browser and Passwords. The app catalogue (`src/i18n/en.ts`) spreads them in after
// `SETTINGS_STRINGS`. All sentences are ours.

export const SETTINGS_A_STRINGS = {
  "settings.view.browser": "Browser",
  "settings.view.passwords": "Passwords & Logins",
  "settings.page.model.moa": "Mixture of Agents",
  "settings.page.appearance.general": "General",
  "settings.page.appearance.chatDisplay": "Chat display",
  "settings.page.appearance.pet": "Pet",
  "settings.page.browser.profile": "Browser profile",
  "settings.page.browser.network": "Local & private URLs",
  "settings.page.passwords.credentials": "Saved credentials",
  "settings.page.passwords.sources": "Password managers",

  "settingsA.footer.export": "Export settings",
  "settingsA.footer.import": "Import settings",
  "settingsA.footer.reset": "Reset to defaults",
  "settingsA.footer.exported": "Settings saved to {name}.",
  "settingsA.footer.imported": "Settings imported.",
  "settingsA.retry": "Retry",
  "settingsA.list.placeholder": "comma-separated",
  "settingsA.search": "Search…",
  "settingsA.searchPill": "Search",

  // field labels and captions (our words; the schema description is the fallback caption)
  "settingsA.field.approvalsMode": "Approval mode",
  "settingsA.field.approvalsModeBody":
    "Manual asks before risky commands; smart lets a judge decide first; off never asks.",
  "settingsA.field.approvalsTimeout": "Approval timeout",
  "settingsA.field.approvalsTimeoutBody":
    "Seconds to wait for your answer before the command is refused.",
  "settingsA.field.mcpReload": "Confirm MCP reloads",
  "settingsA.field.allowlist": "Command allowlist",
  "settingsA.field.redact": "Redact secrets",
  "settingsA.field.redactBody": "Mask keys and tokens in logs and in what tools return.",
  "settingsA.field.privateUrls": "Allow private URLs",
  "settingsA.field.checkpoints": "File checkpoints",
  "settingsA.field.checkpointsBody":
    "Snapshot the working folder before the agent changes files, so a change can be rolled back.",
  "settingsA.field.realProfile": "Use my real browser profile",
  "settingsA.field.realProfileBody":
    "Local browsing starts from a managed copy of your default Chromium profile, refreshed each run, so saved sign-ins work. Your live profile is never opened or changed. Chromium-family browsers only; off by default.",
  "settingsA.field.realProfileDir": "Profile folder",
  "settingsA.field.routeLocal": "Local browser for private URLs",
  "settingsA.field.cwd": "Working directory",
  "settingsA.field.cwdBody": "Where commands run when a chat has no folder of its own.",
  "settingsA.field.persistentShell": "Persistent shell",
  "settingsA.field.persistentShellBody":
    "Variables you export carry over to the next command; the current folder always does.",
  "settingsA.field.envPassthrough": "Environment passthrough",
  "settingsA.field.envPassthroughBody":
    "Extra variable names handed to the commands the agent runs.",
  "settingsA.field.codeMode": "Code execution mode",
  "settingsA.field.codeModeBody":
    "Project runs code in the chat's folder with its environment; strict uses a scratch folder.",
  "settingsA.field.readLimit": "File read limit",
  "settingsA.field.readLimitBody": "The most characters one file read returns to the model.",
  "settingsA.field.timezone": "Timezone",
  "settingsA.field.timezoneBody":
    "Used for dates and times the agent sees. Empty uses this computer's zone.",
  "settingsA.field.reasoning": "Reasoning blocks",
  "settingsA.field.reasoningBody": "Show the model's thinking above its reply when it shares it.",
  "settingsA.field.busyMode": "While a reply runs",
  "settingsA.field.busyModeBody":
    "What Enter does when a reply is still running: interrupt, queue or steer.",
  "settingsA.field.speed": "Speed",
  "settingsA.field.speedBody":
    "The provider's priority tier: normal, always fast, or fast per turn.",
  "settingsA.field.contextWindow": "Main model context window (override)",
  "settingsA.field.contextWindowBody":
    "Overrides the detected window for the main model only. Empty or 0 uses the detected value; side models are not affected.",
  "settingsA.field.minSwitch": "Return to main after",
  "settingsA.field.minSwitchBody":
    "Seconds: stay on the main model when its rate limit resets sooner than this.",
  "settingsA.field.baseUrl": "Base URL",
  "settingsA.field.apiMode": "API mode",
  "settingsA.field.moaTimeout": "Reference timeout",
  "settingsA.field.moaPrivacy": "Privacy filter",

  // Model › Main
  "settingsA.main.intro":
    "New sessions start on this model. The model chip in the composer changes the open chat only.",
  "settingsA.main.provider": "Provider",
  "settingsA.main.model": "Model",
  "settingsA.main.apply": "Apply",
  "settingsA.main.applying": "Applying…",
  "settingsA.main.needsKey": "needs key",
  "settingsA.main.defaults": "Defaults",
  "settingsA.main.reasoning": "Reasoning",
  "settingsA.main.reasoningAuto": "Auto",
  "settingsA.main.optionsFailed": "The provider list could not be read: {message}",

  // Model › Fallbacks
  "settingsA.fallback.title": "Fallback models",
  "settingsA.fallback.body":
    "Backup provider and model pairs, tried in order when the main model fails.",
  "settingsA.fallback.empty":
    "No fallback models yet. Add one to keep chats going when the main model is down.",
  "settingsA.fallback.add": "Add fallback",
  "settingsA.fallback.remove": "Remove",
  "settingsA.fallback.provider": "Provider of fallback {n}",
  "settingsA.fallback.model": "Model of fallback {n}",

  // Model › Auxiliary
  "settingsA.aux.intro": "Side tasks use the main model unless you pick another one here.",
  "settingsA.aux.resetAll": "Reset all to main",
  "settingsA.aux.setMain": "Set to main",
  "settingsA.aux.change": "Change",
  "settingsA.aux.cancel": "Cancel",
  "settingsA.aux.auto": "auto · use main model",
  "settingsA.aux.reasoning": "Reasoning",
  "settingsA.aux.reasoningLevel": "Reasoning {level}",
  "settingsA.aux.inherit": "Inherit",
  "settingsA.aux.more": "More",
  "settingsA.aux.failed": "Not every side task could be changed: {message}",
  "settingsA.aux.vision": "Vision",
  "settingsA.aux.vision.pill": "Image analysis",
  "settingsA.aux.compression": "Compression",
  "settingsA.aux.compression.pill": "Context compaction",
  "settingsA.aux.title": "Title",
  "settingsA.aux.title.pill": "Session titles",
  "settingsA.aux.approval": "Approval",
  "settingsA.aux.approval.pill": "Smart approvals",
  "settingsA.aux.review": "Review",
  "settingsA.aux.review.pill": "Code review",
  "settingsA.aux.curator": "Curator",
  "settingsA.aux.curator.pill": "Skill curation",
  "settingsA.aux.goal_judge": "Goal judge",
  "settingsA.aux.goal_judge.pill": "Goal checks",
  "settingsA.aux.side_question": "Side question",
  "settingsA.aux.side_question.pill": "Quick asides",

  // Model › Mixture of Agents
  "settingsA.moa.intro":
    "Each preset shows up as a model of the Mixture provider. The aggregator answers every step and is billed; the reference models advise once per turn.",
  "settingsA.moa.preset": "Preset",
  "settingsA.moa.enabled": "Enabled",
  "settingsA.moa.setDefault": "Set default",
  "settingsA.moa.delete": "Delete",
  "settingsA.moa.newPreset": "new preset",
  "settingsA.moa.addPreset": "Add preset",
  "settingsA.moa.default": "Default:",
  "settingsA.moa.none": "No presets yet",
  "settingsA.moa.reference": "Reference {n}",
  "settingsA.moa.referencePill": "advises once per turn",
  "settingsA.moa.referenceOn": "Use reference {n}",
  "settingsA.moa.addReference": "Add reference model",
  "settingsA.moa.remove": "Remove",
  "settingsA.moa.aggregator": "Aggregator",
  "settingsA.moa.aggregatorPill": "acting model · billed",
  "settingsA.moa.provider": "Provider",
  "settingsA.moa.model": "Model",

  // Appearance › General
  "settingsA.general.language": "Language",
  "settingsA.general.languageBody": "Indus ships in English for now.",
  "settingsA.general.splash": "Intro splash",
  "settingsA.general.pricing": "Model pricing",
  "settingsA.general.reopen": "Reopen last chat on launch",
  "settingsA.general.tips": "In-app tips",
  "settingsA.general.tours": "Guided tours",

  // Appearance › Theme
  "settingsA.theme.title": "Theme",
  "settingsA.theme.body":
    "Desktop colour palettes. Light, dark or system applies on top of the one you pick.",
  "settingsA.theme.search": "Search themes or the VS Code Marketplace…",
  "settingsA.theme.noMatch": 'No themes match "{query}".',
  "settingsA.theme.remove": "Remove theme",
  "settingsA.theme.marketplace": "From the VS Code Marketplace",
  "settingsA.theme.yours": "A theme you added",
  "settingsA.theme.plugin": "From a plugin",
  "settingsA.theme.desc.indus": "Clay neutrals with a river blue accent.",
  "settingsA.theme.desc.monsoon": "Rain-washed greys and a deep sky blue.",
  "settingsA.theme.desc.laterite": "Warm brick reds on sun-baked earth.",
  "settingsA.theme.desc.saffron": "Golden orange highlights on cream.",
  "settingsA.theme.desc.indigo": "Ink-dyed blues, calm and dense.",
  "settingsA.theme.desc.basalt": "Cool volcanic greys with little colour.",
  "settingsA.theme.desc.lotus": "Soft pinks over pale petal neutrals.",
  "settingsA.theme.desc.mangrove": "Shaded greens from the tidal forest.",
  "settingsA.theme.desc.dune": "Sand tones with a muted ochre accent.",
  "settingsA.theme.desc.harbour": "Sea-blue accents on weathered slate.",
  "settingsA.theme.desc.terrace": "Tea-garden greens on warm stone.",
  "settingsA.theme.desc.jade": "Polished green over smooth light greys.",

  // Appearance › Typography
  "settingsA.type.uiScale": "UI scale",
  "settingsA.type.uiScaleBody": "Makes the whole window larger or smaller. Now {value}.",
  "settingsA.type.chatText": "Chat text size",
  "settingsA.type.chatTextBody":
    "The size of the conversation's text, relative to the interface. Now {value}.",
  "settingsA.type.chatFont": "Chat font",
  "settingsA.type.chatFontBody":
    "A font installed on this computer, or a CSS font stack, for this profile's chats.",
  "settingsA.type.chatFontPlaceholder": "e.g. OpenDyslexic, or a CSS font stack",
  "settingsA.type.useTheme": "Use theme font",
  "settingsA.type.preview": "Preview",
  "settingsA.type.previewText":
    "The quick brown fox jumps over the lazy dog, then reads the changelog.",
  "settingsA.type.terminalFont": "Terminal font",
  "settingsA.type.terminalFontBody": "A monospace font for this profile's terminal pane.",
  "settingsA.type.terminalFontPlaceholder": "e.g. JetBrains Mono, monospace",
  "settingsA.type.useDefault": "Use default",
  "settingsA.type.glyphs": "Glyph preview",

  // Appearance › Window & layout
  "settingsA.window.mode": "Interface mode",
  "settingsA.window.modeBody":
    "Simple shows the chat alone; advanced adds the panes and the status bar.",
  "settingsA.window.simple": "Simple",
  "settingsA.window.advanced": "Advanced",
  "settingsA.window.density": "Session list density",
  "settingsA.window.compact": "Compact",
  "settingsA.window.comfortable": "Comfortable",
  "settingsA.window.detailed": "Detailed",
  "settingsA.window.tabs": "Tab strip",
  "settingsA.window.auto": "Auto",
  "settingsA.window.always": "Always",
  "settingsA.window.never": "Never",
  "settingsA.window.actions": "App actions",
  "settingsA.window.right": "Right",
  "settingsA.window.left": "Left",
  "settingsA.window.backdrop": "Chat backdrop",
  "settingsA.window.files": "File browser",
  "settingsA.window.floating": "Floating composer",

  // Appearance › Chat display
  "settingsA.display.bubble": "Message bubble",
  "settingsA.display.direction": "Text direction",
  "settingsA.display.rtl": "Right-to-left",
  "settingsA.display.ltr": "Left-to-right",
  "settingsA.display.timeline": "Hide thread timeline bars",
  "settingsA.display.reactions": "Message reactions",
  "settingsA.display.hearts": "Vibe hearts",
  "settingsA.display.tools": "Tool call display",
  "settingsA.display.product": "Product",
  "settingsA.display.technical": "Technical",
  "settingsA.display.diffs": "Hide code diffs",
  "settingsA.display.thinking": "Collapse thinking by default",
  "settingsA.display.embeds": "Inline embeds",
  "settingsA.display.ask": "Ask",
  "settingsA.display.off": "Off",

  // Appearance › Pet
  "settingsA.pet.intro":
    "A small animated companion can sit over the app and react to what the agent is doing. Pick one of yours.",
  "settingsA.pet.choose": "Choose a pet",
  "settingsA.pet.chooseBody": "Show the chosen pet over the app.",
  "settingsA.pet.search": "Search pets…",
  "settingsA.pet.showing": "Showing {shown} of {total}. Type to narrow it down.",
  "settingsA.pet.none": "No pets yet",
  "settingsA.pet.noneBody": "Hatch one with /pet in a chat.",
  "settingsA.pet.offline": "Pets are listed once Indus is connected.",

  // Passwords
  "settingsA.vault.title": "Saved credentials",
  "settingsA.vault.add": "Add",
  "settingsA.vault.intro":
    "Ask the agent to sign in somewhere and it asks you for the login once. Passwords stay encrypted on this computer and are typed into the page, never shown to the model.",
  "settingsA.vault.empty": "Nothing saved yet",
  "settingsA.vault.emptyBody":
    "Add logins with indus vault add in a terminal, or let the agent ask in a chat.",
  "settingsA.sources.title": "Password managers",
  "settingsA.sources.intro":
    "Besides this computer's own vault, the agent can fill logins from a password manager's command-line tool. It asks you to unlock it once per session.",
  "settingsA.sources.on": "On",
  "settingsA.sources.off": "Off",
  "settingsA.sources.missing": "Not detected",
  "settingsA.sources.onepasswordMissing":
    "Not found on this computer. Install the 1Password command-line tool (op) and sign in to it; Indus finds it by itself.",
  "settingsA.sources.bitwardenMissing":
    "Not found on this computer. Install the Bitwarden command-line tool (bw) and sign in to it; Indus finds it by itself.",
  "settingsA.sources.local": "This computer",
  "settingsA.sources.localBody": "The encrypted vault Indus keeps in your profile.",
  "settingsA.sources.onepassword": "1Password",
  "settingsA.sources.onepasswordBody":
    "Uses the 1Password command-line tool when it is installed and signed in.",
  "settingsA.sources.bitwarden": "Bitwarden",
  "settingsA.sources.bitwardenBody":
    "Uses the Bitwarden command-line tool when it is installed and signed in.",
} as const satisfies Record<string, string>;
