// The Settings tree (P20-32): views, their child pages, and which page owns which setting. One
// table serves the navigation, the renderer of each page, search and saved links, so a search hit
// or a link can never point at a page that does not show the setting.
//
// A page is either schema-backed (`fields` exact keys and `prefixes` from the backend's config
// schema) or a hand-built page (`custom`); a field belongs to the FIRST page in table order that
// claims it. `also` shows a field owned elsewhere on a second page without moving its ownership.

import type { MessageKey } from "../i18n/index.tsx";

export type CustomPage =
  // settings slice A (W52-i): hand-built pages of Model, Appearance, Workspace, Passwords
  | "model-main"
  | "model-fallbacks"
  | "model-auxiliary"
  | "model-moa"
  | "appearance-general"
  | "appearance-chat-display"
  | "appearance-pet"
  | "workspace-projects"
  | "workspace-files"
  | "passwords-credentials"
  | "passwords-sources"
  | "persona"
  | "appearance-theme"
  | "appearance-typography"
  | "appearance-window"
  | "advanced-desktop"
  | "providers-accounts"
  | "providers-keys"
  | "providers-endpoints"
  | "gateways-connection"
  | "gateways-connections"
  | "keyboard"
  | "notifications"
  | "profiles"
  | "plugins"
  | "about"
  | "about-licences"
  // settings-b (W52-j): Voice, Notifications, Providers, Gateways, Keyboard, Tools & Keys,
  // Sessions and About pages added by that slice
  | "voice-wake"
  | "notifications-sounds"
  | "providers-local"
  | "gateways-updates"
  | "keyboard-global"
  | "tools-keys"
  | "sessions-archived-list"
  | "sessions-directory"
  | "about-uninstall";

export interface PageDef {
  readonly id: string;
  readonly label: MessageKey;
  readonly fields?: readonly string[];
  readonly prefixes?: readonly string[];
  readonly also?: readonly string[];
  readonly custom?: CustomPage;
  /**
   * The fields shown up front, in this order of importance (W51-b); the page's other fields go
   * behind the "More settings" fold. Absent: every field shows.
   */
  readonly curated?: readonly string[];
  /** These fields first, in this order; the page's other fields follow in schema order (W52-i). */
  readonly order?: readonly string[];
  /** A hand-built block rendered below this page's schema rows (W52-j: Voice, Sessions). */
  readonly after?: CustomPage;
}

export const VIEW_IDS = [
  "model",
  "chat",
  "appearance",
  "workspace",
  "safety",
  "browser",
  "passwords",
  // settings-b (W52-j) views, in the reference's order
  "memory",
  "voice",
  "advanced",
  "notifications",
  "providers",
  "gateways",
  "keyboard",
  "tools",
  "sessions",
  "plugins",
  "about",
] as const;
export type ViewId = (typeof VIEW_IDS)[number];

export interface ViewDef {
  readonly id: ViewId;
  readonly label: MessageKey;
  /** Config-backed: the "Applies to" chips show here (two or more profiles). */
  readonly scoped: boolean;
  readonly pages: readonly PageDef[];
}

export const VIEWS: readonly ViewDef[] = [
  {
    id: "model",
    label: "settings.view.model",
    scoped: true,
    pages: [
      {
        id: "main",
        label: "settings.page.model.main",
        custom: "model-main",
        fields: [
          "model.default",
          "model.provider",
          "model.base_url",
          "model.api_mode",
          "model.context_length",
          "agent.fast_mode",
        ],
      },
      {
        id: "fallbacks",
        label: "settings.page.model.fallbacks",
        custom: "model-fallbacks",
        fields: ["fallback_providers"],
        prefixes: ["fallback."],
      },
      {
        id: "auxiliary",
        label: "settings.page.model.auxiliary",
        custom: "model-auxiliary",
        prefixes: ["auxiliary."],
      },
      { id: "moa", label: "settings.page.model.moa", custom: "model-moa", prefixes: ["moa."] },
    ],
  },
  {
    id: "chat",
    label: "settings.view.chat",
    scoped: true,
    pages: [
      {
        id: "behaviour",
        label: "settings.page.chat.behaviour",
        fields: ["display.show_reasoning", "display.busy_input_mode", "timezone"],
        order: ["timezone", "display.show_reasoning", "display.busy_input_mode"],
      },
      { id: "personality", label: "settings.page.chat.personality", custom: "persona" },
      // the wake word moved to the Voice view (W52-j, lead decision "Voice is its own item")
    ],
  },
  {
    id: "appearance",
    label: "settings.view.appearance",
    scoped: false,
    pages: [
      { id: "general", label: "settings.page.appearance.general", custom: "appearance-general" },
      { id: "theme", label: "settings.page.appearance.theme", custom: "appearance-theme" },
      {
        id: "typography",
        label: "settings.page.appearance.typography",
        custom: "appearance-typography",
      },
      { id: "window", label: "settings.page.appearance.window", custom: "appearance-window" },
      {
        id: "chat-display",
        label: "settings.page.appearance.chatDisplay",
        custom: "appearance-chat-display",
      },
      { id: "pet", label: "settings.page.appearance.pet", custom: "appearance-pet" },
    ],
  },
  {
    id: "workspace",
    label: "settings.view.workspace",
    scoped: true,
    pages: [
      {
        id: "projects",
        label: "settings.page.workspace.projects",
        custom: "workspace-projects",
        fields: ["terminal.cwd"],
      },
      {
        id: "shell",
        label: "settings.page.workspace.shell",
        // terminal.backend / terminal.timeout moved to Advanced › Terminal backend (W52-B-3)
        fields: ["terminal.persistent_shell", "terminal.env_passthrough"],
      },
      {
        id: "files",
        label: "settings.page.workspace.files",
        custom: "workspace-files",
        // tool_output.max_chars moves to Advanced › Output limits (slice B, W52-B-3)
        fields: ["code_execution.mode", "file_read_max_chars"],
      },
    ],
  },
  {
    id: "safety",
    label: "settings.view.safety",
    scoped: true,
    pages: [
      {
        id: "approvals",
        label: "settings.page.safety.approvals",
        fields: ["command_allowlist"],
        prefixes: ["approvals."],
        order: [
          "approvals.mode",
          "approvals.timeout",
          "approvals.mcp_reload_confirm",
          "command_allowlist",
        ],
      },
      {
        id: "privacy",
        label: "settings.page.safety.privacy",
        prefixes: ["security."],
        order: ["security.redact_secrets", "security.allow_private_urls"],
      },
      {
        id: "checkpoints",
        label: "settings.page.safety.checkpoints",
        prefixes: ["checkpoints."],
        curated: ["checkpoints.enabled"],
      },
    ],
  },
  {
    id: "browser",
    label: "settings.view.browser",
    scoped: true,
    pages: [
      {
        id: "profile",
        label: "settings.page.browser.profile",
        // the reference rows first; our other browser.* fields (engine, timeouts, providers) behind
        // the fold (W52-dsa-14). Listed by name: browser.route_private_local and
        // browser.vault_sources belong to the next pages.
        fields: [
          "browser.real_profile",
          "browser.real_profile_dir",
          "browser.provider",
          "browser.engine",
          "browser.inactivity_timeout",
          "browser.command_timeout",
          "browser.snapshot_threshold",
          "browser.snapshot_summary",
          "browser.cdp_url",
          "browser.executable_path",
          "browser.env_passthrough",
          "browser.dialog_policy",
          "browser.dialog_timeout_s",
          "browser.camofox_url",
          "browser.control_enabled",
          "browser.control_developer_mode",
        ],
        curated: ["browser.real_profile", "browser.real_profile_dir"],
      },
      {
        id: "network",
        label: "settings.page.browser.network",
        fields: ["browser.route_private_local"],
        also: ["security.allow_private_urls"],
      },
    ],
  },
  {
    id: "passwords",
    label: "settings.view.passwords",
    scoped: true,
    pages: [
      {
        id: "credentials",
        label: "settings.page.passwords.credentials",
        custom: "passwords-credentials",
      },
      {
        id: "sources",
        label: "settings.page.passwords.sources",
        custom: "passwords-sources",
        fields: ["browser.vault_sources"],
      },
    ],
  },
  // -- settings-b (W52-j) views ------------------------------------------------------------------
  {
    id: "memory",
    label: "settings.view.memory",
    scoped: true,
    pages: [
      {
        id: "persistent",
        label: "settings.page.memory.persistent",
        prefixes: ["memory."],
        curated: [
          "memory.notes_enabled",
          "memory.profile_enabled",
          "memory.notes_char_limit",
          "memory.profile_char_limit",
          "memory.provider",
        ],
      },
      {
        id: "context",
        label: "settings.page.memory.context",
        prefixes: ["compression.", "context."],
        curated: [
          "context.engine",
          "compression.enabled",
          "compression.threshold",
          "compression.target_ratio",
          "compression.min_tail_user_messages",
          "compression.timeout_s",
        ],
      },
    ],
  },
  {
    id: "voice",
    label: "settings.view.voice",
    scoped: true,
    pages: [
      {
        id: "conversation",
        label: "settings.page.voice.conversation",
        fields: [
          "voice.record_key",
          "voice.max_seconds",
          "voice.silence_threshold",
          "voice.silence_seconds",
          "voice.wait_seconds",
        ],
        curated: ["voice.record_key", "voice.max_seconds"],
        after: "voice-wake",
      },
      {
        id: "transcription",
        label: "settings.page.voice.transcription",
        prefixes: ["stt."],
        curated: ["stt.provider", "stt.local_model", "stt.language", "stt.model"],
      },
      {
        id: "speech",
        label: "settings.page.voice.speech",
        fields: ["voice.read_aloud"],
        prefixes: ["tts."],
        curated: ["tts.provider", "voice.read_aloud", "tts.voice", "tts.speed"],
      },
    ],
  },
  {
    id: "advanced",
    label: "settings.view.advanced",
    scoped: true,
    pages: [
      { id: "desktop", label: "settings.page.advanced.desktop", custom: "advanced-desktop" },
      {
        id: "runtime",
        label: "settings.page.advanced.runtime",
        prefixes: ["agent."],
        curated: ["agent.max_turns", "agent.api_max_retries"],
      },
      {
        id: "tools",
        label: "settings.page.advanced.tools",
        fields: ["prompt.tool_use_enforcement"],
        prefixes: ["code_execution."],
        curated: [
          "prompt.tool_use_enforcement",
          "code_execution.timeout",
          "code_execution.max_tool_calls",
        ],
      },
      {
        id: "terminal",
        label: "settings.page.advanced.terminal",
        prefixes: ["terminal."],
        curated: [
          "terminal.backend",
          "terminal.timeout",
          "terminal.docker_image",
          "terminal.singularity_image",
          "terminal.modal_image",
          "terminal.daytona_image",
        ],
      },
      {
        id: "delegation",
        label: "settings.page.advanced.delegation",
        prefixes: ["delegation."],
        curated: [
          "delegation.model",
          "delegation.max_iterations",
          "delegation.max_concurrent_children",
          "delegation.max_depth",
        ],
      },
      {
        id: "output",
        label: "settings.page.advanced.output",
        fields: ["tool_output.max_chars"],
        also: ["checkpoints.max_snapshots"],
      },
      { id: "profiles", label: "settings.page.advanced.profiles", custom: "profiles" },
    ],
  },
  {
    id: "notifications",
    label: "settings.view.notifications",
    scoped: false,
    pages: [
      { id: "alerts", label: "settings.page.notifications.alerts", custom: "notifications" },
      { id: "sounds", label: "settings.page.notifications.sounds", custom: "notifications-sounds" },
    ],
  },
  {
    id: "providers",
    label: "settings.view.providers",
    scoped: true,
    pages: [
      { id: "accounts", label: "settings.page.providers.accounts", custom: "providers-accounts" },
      { id: "keys", label: "settings.page.providers.keys", custom: "providers-keys" },
      {
        id: "endpoints",
        label: "settings.page.providers.endpoints",
        custom: "providers-endpoints",
      },
      { id: "local", label: "settings.page.providers.local", custom: "providers-local" },
    ],
  },
  {
    id: "gateways",
    label: "settings.view.gateways",
    scoped: false,
    pages: [
      {
        id: "connection",
        label: "settings.page.gateways.connection",
        custom: "gateways-connection",
      },
      {
        id: "connections",
        label: "settings.page.gateways.connections",
        custom: "gateways-connections",
      },
      { id: "updates", label: "settings.page.gateways.updates", custom: "gateways-updates" },
    ],
  },
  {
    id: "keyboard",
    label: "settings.view.keyboard",
    scoped: false,
    pages: [
      { id: "shortcuts", label: "settings.page.keyboard.shortcuts", custom: "keyboard" },
      { id: "global", label: "settings.page.keyboard.global", custom: "keyboard-global" },
    ],
  },
  {
    id: "tools",
    label: "settings.view.tools",
    scoped: true,
    pages: [{ id: "keys", label: "settings.page.tools.keys", custom: "tools-keys" }],
  },
  {
    id: "sessions",
    label: "settings.view.sessions",
    scoped: true,
    pages: [
      {
        id: "archived",
        label: "settings.page.sessions.archived",
        prefixes: ["sessions."],
        curated: ["sessions.auto_prune", "sessions.retention_days"],
        after: "sessions-archived-list",
      },
      { id: "directory", label: "settings.page.sessions.directory", custom: "sessions-directory" },
    ],
  },
  {
    id: "plugins",
    label: "settings.view.plugins",
    scoped: false,
    pages: [{ id: "main", label: "settings.page.plugins.main", custom: "plugins" }],
  },
  {
    id: "about",
    label: "settings.view.about",
    scoped: false,
    pages: [
      { id: "version", label: "settings.page.about.version", custom: "about" },
      { id: "uninstall", label: "settings.page.about.uninstall", custom: "about-uninstall" },
      { id: "licences", label: "settings.page.about.licences", custom: "about-licences" },
    ],
  },
];

const BY_ID: ReadonlyMap<ViewId, ViewDef> = new Map(VIEWS.map((v) => [v.id, v]));

export function viewDef(id: string | null | undefined): ViewDef | null {
  return (id !== null && id !== undefined && BY_ID.get(id as ViewId)) || null;
}

function claims(page: PageDef, field: string): boolean {
  return (
    (page.fields?.includes(field) ?? false) ||
    (page.prefixes?.some((prefix) => field.startsWith(prefix)) ?? false)
  );
}

/** The page that owns `field` (first claim in table order), or null when no page shows it. */
export function pageForField(field: string): { view: ViewDef; page: PageDef } | null {
  for (const view of VIEWS) {
    for (const page of view.pages) if (claims(page, field)) return { view, page };
  }
  return null;
}

/** The schema fields `page` renders, in schema order: its own and its `also` fields. */
export function fieldsOnPage(page: PageDef, schemaFields: readonly string[]): string[] {
  return schemaFields.filter((field) => {
    if (page.also?.includes(field)) return true;
    const owner = pageForField(field);
    return owner !== null && owner.page.id === page.id && claims(page, field);
  });
}

/** Old page ids that still land somewhere (saved links, deep links): view → old id → page. */
const PAGE_ALIASES: Readonly<Partial<Record<ViewId, Readonly<Record<string, string>>>>> = {
  workspace: { desktop: "projects" }, // W52-i: "On this computer" merged into Projects & discovery
};

export interface SettingsTarget {
  readonly view: ViewId;
  readonly page: string;
  /** A schema field to scroll to and outline. */
  readonly field: string | null;
  /** A hand-built row (manifest id) to scroll to. */
  readonly row: string | null;
}

/**
 * Where a settings link lands: `view`, `page`, `field`, `row` query values. A field alone finds
 * its owner; a page outside the view, or none, lands on the view's first page; an unknown view on
 * the first view.
 */
export function resolveTarget(query: string | URLSearchParams): SettingsTarget {
  const params = typeof query === "string" ? new URLSearchParams(query) : query;
  const field = params.get("field");
  const row = params.get("row");
  let view = viewDef(params.get("view"));
  let pageId = params.get("page");
  if (field !== null && field !== "") {
    const owner = pageForField(field);
    if (owner !== null && (view === null || view === owner.view)) {
      view = owner.view;
      pageId = owner.page.id;
    }
  }
  if (row !== null && row !== "" && view === null) {
    const found = MANIFEST.find((r) => r.id === row);
    if (found !== undefined) {
      view = viewDef(found.view);
      pageId = found.page;
    }
  }
  const resolved = view ?? (VIEWS[0] as ViewDef);
  const alias = pageId === null ? undefined : PAGE_ALIASES[resolved.id]?.[pageId];
  const page =
    resolved.pages.find((p) => p.id === (alias ?? pageId)) ?? (resolved.pages[0] as PageDef);
  return {
    view: resolved.id,
    page: page.id,
    field: field === "" ? null : field,
    row: row === "" ? null : row,
  };
}

/** The query string a target is stored as (`/settings?<this>`). */
export function targetQuery(target: {
  view: ViewId;
  page?: string;
  field?: string | null;
  row?: string | null;
}): string {
  const params = new URLSearchParams({ view: target.view });
  if (target.page !== undefined) params.set("page", target.page);
  if (target.field) params.set("field", target.field);
  if (target.row) params.set("row", target.row);
  return params.toString();
}

/** A hand-built settings row: searchable, linkable (`row=<id>`), shown by its custom page. */
export interface ManifestRow {
  readonly id: string;
  readonly view: ViewId;
  readonly page: string;
  readonly label: MessageKey;
  readonly description?: MessageKey;
  readonly keywords: readonly string[];
}

export const MANIFEST: readonly ManifestRow[] = [
  {
    id: "persona",
    view: "chat",
    page: "personality",
    label: "settings.persona.title",
    description: "settings.persona.body",
    keywords: ["personality", "persona", "soul", "instructions", "tone"],
  },
  {
    id: "theme",
    view: "appearance",
    page: "theme",
    label: "settings.theme.title",
    keywords: ["dark", "light", "colour", "color", "mode"],
  },
  {
    id: "ui-scale",
    view: "appearance",
    page: "typography",
    label: "settings.uiScale.title",
    description: "settings.uiScale.body",
    keywords: ["zoom", "size", "scale", "bigger", "smaller"],
  },
  {
    id: "chat-text",
    view: "appearance",
    page: "typography",
    label: "settings.chatText.title",
    description: "settings.chatText.body",
    keywords: ["font", "text", "size", "chat"],
  },
  {
    id: "tray",
    view: "appearance",
    page: "window",
    label: "settings.tray.title",
    description: "settings.tray.body",
    keywords: ["tray", "minimise", "minimize", "background", "menu bar"],
  },
  {
    id: "update-channel",
    view: "about",
    page: "version",
    label: "settings.updates.channel",
    keywords: ["updates", "canary", "stable", "release", "beta"],
  },
  {
    id: "keep-awake",
    view: "advanced",
    page: "desktop",
    label: "settings.keepAwake.title",
    description: "settings.keepAwake.body",
    keywords: ["sleep", "awake", "power", "caffeinate"],
  },
  {
    id: "quick-entry",
    view: "advanced",
    page: "desktop",
    label: "settings.quickEntry.title",
    description: "settings.quickEntry.body",
    keywords: ["global", "shortcut", "hotkey", "quick", "launcher"],
  },
  {
    id: "device-sign-in",
    view: "providers",
    page: "accounts",
    label: "settings.accounts.title",
    keywords: ["sign in", "login", "oauth", "device code", "account"],
  },
  {
    id: "api-keys",
    view: "providers",
    page: "keys",
    label: "settings.keys.title",
    keywords: ["api key", "token", "credentials", "secret"],
  },
  {
    id: "custom-endpoints",
    view: "providers",
    page: "endpoints",
    label: "settings.endpoints.title",
    keywords: ["openai compatible", "base url", "local", "ollama", "vllm", "llama.cpp", "endpoint"],
  },
  {
    id: "connection-mode",
    view: "gateways",
    page: "connection",
    label: "settings.gateway.mode",
    keywords: ["remote", "local", "server", "connection", "url", "token"],
  },
  {
    id: "secure-storage",
    view: "gateways",
    page: "connection",
    label: "settings.storage.title",
    description: "settings.storage.body",
    keywords: ["keychain", "keyring", "encryption", "secure storage", "safe storage"],
  },
  {
    id: "keybindings",
    view: "keyboard",
    page: "shortcuts",
    label: "settings.keyboard.title",
    keywords: ["shortcut", "hotkey", "keybinding", "rebind", "keys"],
  },
  {
    id: "notify-turn",
    view: "notifications",
    page: "alerts",
    label: "settings.notify.replyReady",
    keywords: ["notification", "alert", "done", "finished"],
  },
  {
    id: "notify-sounds",
    view: "notifications",
    page: "sounds",
    label: "settings.sounds.title",
    keywords: ["sound", "audio", "chime"],
  },
  {
    id: "archived-sessions",
    view: "sessions",
    page: "archived",
    label: "settings.archived.title",
    keywords: ["archive", "restore", "history", "conversations"],
  },
  {
    id: "version",
    view: "about",
    page: "version",
    label: "settings.about.title",
    keywords: ["version", "update", "check", "release", "about"],
  },
  {
    id: "licences",
    view: "about",
    page: "licences",
    label: "settings.licences.title",
    keywords: [
      "licences",
      "licenses",
      "license",
      "notices",
      "open source",
      "third party",
      "credits",
    ],
  },
  {
    id: "logs",
    view: "about",
    page: "version",
    label: "settings.logs.title",
    keywords: ["logs", "log file", "diagnostics", "debug", "errors"],
  },
  {
    id: "uninstall",
    view: "about",
    page: "uninstall",
    label: "settings.uninstall.title",
    keywords: ["uninstall", "remove", "delete", "app", "everything"],
  },
  {
    id: "default-directory",
    view: "workspace",
    page: "projects",
    label: "settings.workspace.defaultDir",
    description: "settings.workspace.defaultDirBody",
    keywords: ["folder", "directory", "new chat", "working folder", "cwd"],
  },
  {
    id: "repo-scan",
    view: "workspace",
    page: "projects",
    label: "settings.workspace.scan",
    description: "settings.workspace.scanBody",
    keywords: ["repositories", "repos", "git", "scan", "discover", "projects"],
  },
  {
    id: "data-url-limit",
    view: "workspace",
    page: "files",
    label: "settings.workspace.dataUrl",
    description: "settings.workspace.dataUrlBody",
    keywords: ["file size", "limit", "image", "attachment", "read"],
  },
  {
    id: "desktop-plugins",
    view: "plugins",
    page: "main",
    label: "plugins.title",
    description: "plugins.body",
    keywords: ["plugins", "extensions", "add-ons", "desktop plugins", "sidebar", "panes"],
  },
  {
    id: "profiles",
    view: "advanced",
    page: "profiles",
    label: "settings.profiles.title",
    description: "settings.profiles.body",
    keywords: ["profile", "rename", "delete", "remove"],
  },
  // -- settings slice A (W52-i) --------------------------------------------------------------------
  {
    id: "moa-presets",
    view: "model",
    page: "moa",
    label: "settingsA.moa.preset",
    description: "settingsA.moa.intro",
    keywords: ["mixture", "agents", "moa", "preset", "aggregator", "reference models"],
  },
  {
    id: "language",
    view: "appearance",
    page: "general",
    label: "settingsA.general.language",
    keywords: ["language", "locale", "english", "translation"],
  },
  {
    id: "interface-mode",
    view: "appearance",
    page: "window",
    label: "settingsA.window.mode",
    keywords: ["simple", "advanced", "interface", "layout", "panes"],
  },
  // W55-F3: the renderer-only display preferences
  {
    id: "intro-splash",
    view: "appearance",
    page: "general",
    label: "settingsA.general.splash",
    description: "settingsA.general.splashBody",
    keywords: ["splash", "wordmark", "empty chat", "intro"],
  },
  {
    id: "reopen-last-chat",
    view: "appearance",
    page: "general",
    label: "settingsA.general.reopen",
    description: "settingsA.general.reopenBody",
    keywords: ["launch", "startup", "restore", "last chat", "reopen"],
  },
  {
    id: "session-density",
    view: "appearance",
    page: "window",
    label: "settingsA.window.density",
    description: "settingsA.window.densityBody",
    keywords: ["sidebar", "density", "compact", "detailed", "sessions"],
  },
  {
    id: "tab-strip",
    view: "appearance",
    page: "window",
    label: "settingsA.window.tabs",
    description: "settingsA.window.tabsBody",
    keywords: ["tabs", "tab strip", "panes", "zones"],
  },
  {
    id: "chat-backdrop",
    view: "appearance",
    page: "window",
    label: "settingsA.window.backdrop",
    description: "settingsA.window.backdropBody",
    keywords: ["backdrop", "background", "wordmark", "watermark"],
  },
  {
    id: "message-bubble",
    view: "appearance",
    page: "chat-display",
    label: "settingsA.display.bubble",
    description: "settingsA.display.bubbleBody",
    keywords: ["bubble", "transparent", "see-through", "outline", "message"],
  },
  {
    id: "text-direction",
    view: "appearance",
    page: "chat-display",
    label: "settingsA.display.direction",
    description: "settingsA.display.directionBody",
    keywords: ["rtl", "ltr", "right-to-left", "arabic", "hebrew", "urdu", "direction"],
  },
  {
    id: "timeline-bars",
    view: "appearance",
    page: "chat-display",
    label: "settingsA.display.timeline",
    description: "settingsA.display.timelineBody",
    keywords: ["timeline", "markers", "rail", "navigation"],
  },
  {
    id: "tool-call-display",
    view: "appearance",
    page: "chat-display",
    label: "settingsA.display.tools",
    description: "settingsA.display.toolsBody",
    keywords: ["tool", "tool calls", "arguments", "payload", "technical"],
  },
  {
    id: "collapse-thinking",
    view: "appearance",
    page: "chat-display",
    label: "settingsA.display.thinking",
    description: "settingsA.display.thinkingBody",
    keywords: ["thinking", "reasoning", "collapse"],
  },
  {
    id: "chat-font",
    view: "appearance",
    page: "typography",
    label: "settingsA.type.chatFont",
    keywords: ["font", "typeface", "dyslexic", "chat font"],
  },
  {
    id: "terminal-font",
    view: "appearance",
    page: "typography",
    label: "settingsA.type.terminalFont",
    keywords: ["font", "monospace", "terminal font"],
  },
  {
    id: "pet",
    view: "appearance",
    page: "pet",
    label: "settingsA.pet.choose",
    keywords: ["pet", "companion", "mascot", "buddy"],
  },
  {
    id: "saved-credentials",
    view: "passwords",
    page: "credentials",
    label: "settingsA.vault.title",
    keywords: ["password", "login", "vault", "credentials", "sign in"],
  },
  {
    id: "password-managers",
    view: "passwords",
    page: "sources",
    label: "settingsA.sources.title",
    keywords: ["1password", "bitwarden", "password manager", "vault"],
  },
  // -- settings-b (W52-j) rows --------------------------------------------------------------------
  {
    id: "wake-word",
    view: "voice",
    page: "conversation",
    label: "settings.wake.title",
    keywords: ["wake word", "wake phrase", "hands free", "listen", "microphone", "voice"],
  },
  {
    id: "notify-approvals",
    view: "notifications",
    page: "alerts",
    label: "settings.notify.approvalNeeded",
    keywords: ["notification", "approval", "permission", "waiting"],
  },
  {
    id: "notify-test",
    view: "notifications",
    page: "alerts",
    label: "settings.notify.test",
    keywords: ["notification", "test", "try"],
  },
  {
    id: "local-models",
    view: "providers",
    page: "local",
    label: "settings.local.title",
    keywords: ["local", "ollama", "vllm", "llama.cpp", "lm studio", "offline", "model server"],
  },
  {
    id: "gateway-logs",
    view: "gateways",
    page: "connection",
    label: "settings.gatewayDiag.title",
    keywords: ["logs", "diagnostics", "log file"],
  },
  {
    id: "launch-mode",
    view: "gateways",
    page: "connections",
    label: "settings.launchMode.title",
    keywords: ["startup", "launch", "last used", "primary", "connection"],
  },
  {
    id: "global-shortcuts",
    view: "keyboard",
    page: "global",
    label: "settings.globalKeys.title",
    keywords: ["global", "hud", "quick entry", "system wide", "hotkey", "shortcut"],
  },
  {
    id: "tool-keys",
    view: "tools",
    page: "keys",
    label: "settings.toolKeys.title",
    keywords: ["tool", "skill", "api key", "token", "secret", "credentials", "variable"],
  },
  {
    id: "sessions-directory",
    view: "sessions",
    page: "directory",
    label: "settings.sessionDir.title",
    keywords: ["folder", "directory", "project", "new session", "working folder"],
  },
  {
    id: "run-onboarding",
    view: "advanced",
    page: "desktop",
    label: "settings.onboardingAgain.title",
    keywords: ["onboarding", "setup", "first run", "welcome"],
  },
];
