// The Settings tree (P20-32): views, their child pages, and which page owns which setting. One
// table serves the navigation, the renderer of each page, search and saved links, so a search hit
// or a link can never point at a page that does not show the setting.
//
// A page is either schema-backed (`fields` exact keys and `prefixes` from the backend's config
// schema) or a hand-built page (`custom`); a field belongs to the FIRST page in table order that
// claims it. `also` shows a field owned elsewhere on a second page without moving its ownership.

import type { MessageKey } from "../i18n/index.tsx";

export type CustomPage =
  | "persona"
  | "chat-voice"
  | "appearance-theme"
  | "appearance-typography"
  | "appearance-window"
  | "advanced-desktop"
  | "providers-accounts"
  | "providers-keys"
  | "providers-endpoints"
  | "gateways-connection"
  | "gateways-connections"
  | "gateways-storage"
  | "keyboard"
  | "notifications"
  | "sessions-archived"
  | "workspace-desktop"
  | "profiles"
  | "about";

export interface PageDef {
  readonly id: string;
  readonly label: MessageKey;
  readonly fields?: readonly string[];
  readonly prefixes?: readonly string[];
  readonly also?: readonly string[];
  readonly custom?: CustomPage;
}

export const VIEW_IDS = [
  "model",
  "chat",
  "appearance",
  "workspace",
  "safety",
  "memory",
  "advanced",
  "providers",
  "gateways",
  "keyboard",
  "notifications",
  "sessions",
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
        fields: ["fallback_providers"],
        prefixes: ["fallback."],
      },
      { id: "auxiliary", label: "settings.page.model.auxiliary", prefixes: ["auxiliary."] },
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
      },
      { id: "personality", label: "settings.page.chat.personality", custom: "persona" },
      { id: "voice", label: "settings.page.chat.voice", custom: "chat-voice" }, // P24-20, P24-35
    ],
  },
  {
    id: "appearance",
    label: "settings.view.appearance",
    scoped: false,
    pages: [
      { id: "theme", label: "settings.page.appearance.theme", custom: "appearance-theme" },
      {
        id: "typography",
        label: "settings.page.appearance.typography",
        custom: "appearance-typography",
      },
      { id: "window", label: "settings.page.appearance.window", custom: "appearance-window" },
    ],
  },
  {
    id: "workspace",
    label: "settings.view.workspace",
    scoped: true,
    pages: [
      { id: "projects", label: "settings.page.workspace.projects", fields: ["terminal.cwd"] },
      { id: "desktop", label: "settings.page.workspace.desktop", custom: "workspace-desktop" },
      {
        id: "shell",
        label: "settings.page.workspace.shell",
        fields: [
          "terminal.backend",
          "terminal.persistent_shell",
          "terminal.env_passthrough",
          "terminal.timeout",
        ],
      },
      {
        id: "files",
        label: "settings.page.workspace.files",
        fields: ["file_read_max_chars", "tool_output.max_chars"],
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
      },
      { id: "privacy", label: "settings.page.safety.privacy", prefixes: ["security."] },
      { id: "checkpoints", label: "settings.page.safety.checkpoints", prefixes: ["checkpoints."] },
    ],
  },
  {
    id: "memory",
    label: "settings.view.memory",
    scoped: true,
    pages: [
      { id: "persistent", label: "settings.page.memory.persistent", prefixes: ["memory."] },
      {
        id: "context",
        label: "settings.page.memory.context",
        prefixes: ["compression.", "context."],
      },
    ],
  },
  {
    id: "advanced",
    label: "settings.view.advanced",
    scoped: true,
    pages: [
      { id: "desktop", label: "settings.page.advanced.desktop", custom: "advanced-desktop" },
      { id: "runtime", label: "settings.page.advanced.runtime", prefixes: ["agent."] },
      {
        id: "tools",
        label: "settings.page.advanced.tools",
        prefixes: ["code_execution.", "delegation."],
      },
      { id: "terminal", label: "settings.page.advanced.terminal", prefixes: ["terminal."] },
      { id: "profiles", label: "settings.page.advanced.profiles", custom: "profiles" },
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
      { id: "storage", label: "settings.page.gateways.storage", custom: "gateways-storage" },
    ],
  },
  {
    id: "keyboard",
    label: "settings.view.keyboard",
    scoped: false,
    pages: [{ id: "shortcuts", label: "settings.page.keyboard.shortcuts", custom: "keyboard" }],
  },
  {
    id: "notifications",
    label: "settings.view.notifications",
    scoped: false,
    pages: [{ id: "alerts", label: "settings.page.notifications.alerts", custom: "notifications" }],
  },
  {
    id: "sessions",
    label: "settings.view.sessions",
    scoped: true,
    pages: [
      { id: "archived", label: "settings.page.sessions.archived", custom: "sessions-archived" },
      {
        id: "directory",
        label: "settings.page.sessions.directory",
        also: ["terminal.cwd"],
        prefixes: ["sessions."],
      },
    ],
  },
  {
    id: "about",
    label: "settings.view.about",
    scoped: false,
    pages: [{ id: "version", label: "settings.page.about.version", custom: "about" }],
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
  const page = resolved.pages.find((p) => p.id === pageId) ?? (resolved.pages[0] as PageDef);
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
    view: "advanced",
    page: "desktop",
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
    page: "storage",
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
    label: "settings.notify.turnComplete",
    keywords: ["notification", "alert", "done", "finished"],
  },
  {
    id: "notify-sounds",
    view: "notifications",
    page: "alerts",
    label: "settings.notify.sounds",
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
    id: "logs",
    view: "about",
    page: "version",
    label: "settings.logs.title",
    keywords: ["logs", "log file", "diagnostics", "debug", "errors"],
  },
  {
    id: "uninstall",
    view: "about",
    page: "version",
    label: "settings.uninstall.title",
    keywords: ["uninstall", "remove", "delete", "app", "everything"],
  },
  {
    id: "default-directory",
    view: "workspace",
    page: "desktop",
    label: "settings.workspace.defaultDir",
    description: "settings.workspace.defaultDirBody",
    keywords: ["folder", "directory", "new chat", "working folder", "cwd"],
  },
  {
    id: "repo-scan",
    view: "workspace",
    page: "desktop",
    label: "settings.workspace.scan",
    description: "settings.workspace.scanBody",
    keywords: ["repositories", "repos", "git", "scan", "discover", "projects"],
  },
  {
    id: "data-url-limit",
    view: "workspace",
    page: "desktop",
    label: "settings.workspace.dataUrl",
    description: "settings.workspace.dataUrlBody",
    keywords: ["file size", "limit", "image", "attachment", "read"],
  },
  {
    id: "profiles",
    view: "advanced",
    page: "profiles",
    label: "settings.profiles.title",
    description: "settings.profiles.body",
    keywords: ["profile", "rename", "delete", "remove"],
  },
];
