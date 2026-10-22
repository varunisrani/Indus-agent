// The desktop's appearance choices (P24-33), kept per device in the renderer's storage:
//
// - a preset (`@indus/ui` presets) or a user theme (imported from VS Code or written by hand), an
//   optional accent hue, and per-profile overrides (preset, chat font, terminal font);
// - `resolve(profile, mode)` gives the semantic theme to paint, `css(profile, mode)` its variables.
//
// Nothing is painted until the person chooses something: with no choice the built-in Indus light
// and dark themes stay as they are. `syncTui` writes the polarity to the backend's `tui.theme`, so
// the terminal UI follows (its colour payload is a follow-up).

import {
  type PresetMode,
  presetIds,
  presetTheme,
  type SemanticColour,
  type SemanticName,
  type SemanticTheme,
  semanticNames,
  themeCss,
} from "@indus/ui";
import { type RestLike, rest } from "../settings/settingsApi.ts";
import type { AnsiKey, ConvertedTheme } from "./vscodeImport.ts";

/** Window-wide choices (theme, accent, user themes). */
export const APPEARANCE_KEY = "indus-desktop-appearance";
/** Per-profile overrides: a `record` family (`{ [profile]: … }`), moved on a profile rename. */
export const PROFILE_APPEARANCE_KEY = "indus.desktop.appearance.profiles";

export interface StoredUserTheme {
  readonly id: string;
  readonly label: string;
  /** The side the theme was made for; the other mode uses it too. */
  readonly mode: PresetMode;
  readonly colours: Readonly<Record<SemanticName, string>>;
  readonly terminal: Readonly<Record<AnsiKey, string>> | null;
  /** `publisher.extension` when installed from the Marketplace. */
  readonly origin: string | null;
}

export interface ProfileAppearance {
  readonly theme?: string;
  readonly chatFont?: string;
  readonly terminalFont?: string;
}

export interface Appearance {
  /** A preset id, `user:<id>`, or null for the built-in look. */
  readonly theme: string | null;
  readonly accentHue: number | null;
  readonly userThemes: readonly StoredUserTheme[];
  readonly profiles: Readonly<Record<string, ProfileAppearance>>;
}

export const EMPTY_APPEARANCE: Appearance = {
  theme: null,
  accentHue: null,
  userThemes: [],
  profiles: {},
};

const FONT = /^[\w ,'"-]{1,120}$/;

function clean(raw: unknown): Appearance {
  if (raw === null || typeof raw !== "object") return EMPTY_APPEARANCE;
  const data = raw as Record<string, unknown>;
  const users = Array.isArray(data.userThemes)
    ? (data.userThemes as StoredUserTheme[]).filter(
        (u) =>
          typeof u?.id === "string" &&
          typeof u.label === "string" &&
          u.colours !== null &&
          typeof u.colours === "object" &&
          semanticNames.every((n) => typeof u.colours[n] === "string"),
      )
    : [];
  const profiles: Record<string, ProfileAppearance> = {};
  if (data.profiles !== null && typeof data.profiles === "object") {
    for (const [name, value] of Object.entries(data.profiles as Record<string, unknown>)) {
      if (value === null || typeof value !== "object") continue;
      const entry = value as Record<string, unknown>;
      profiles[name] = {
        ...(typeof entry.theme === "string" ? { theme: entry.theme } : {}),
        ...(typeof entry.chatFont === "string" && FONT.test(entry.chatFont)
          ? { chatFont: entry.chatFont }
          : {}),
        ...(typeof entry.terminalFont === "string" && FONT.test(entry.terminalFont)
          ? { terminalFont: entry.terminalFont }
          : {}),
      };
    }
  }
  return {
    theme: typeof data.theme === "string" ? data.theme : null,
    accentHue:
      typeof data.accentHue === "number" && Number.isFinite(data.accentHue) ? data.accentHue : null,
    userThemes: users,
    profiles,
  };
}

export function userThemeFrom(converted: ConvertedTheme, origin: string | null): StoredUserTheme {
  return {
    id: converted.id,
    label: converted.label,
    mode: converted.mode,
    colours: Object.fromEntries(semanticNames.map((n) => [n, converted.theme[n].hex])) as Record<
      SemanticName,
      string
    >,
    terminal: converted.terminal,
    origin,
  };
}

function fromColours(user: StoredUserTheme): SemanticTheme {
  return Object.fromEntries(
    semanticNames.map((n): [SemanticName, SemanticColour] => [
      n,
      { css: user.colours[n], hex: user.colours[n], source: `user.${user.id}` },
    ]),
  ) as SemanticTheme;
}

/**
 * A theme the contribution registry offers (`theme` area, P24-32): a bundled or plugin theme with
 * both sides resolved. Held in memory only; a choice of it is stored as `contrib:<id>` and paints
 * the built-in look while its plugin is not loaded.
 */
export interface ContributedTheme {
  readonly id: string;
  readonly label: string;
  readonly light: SemanticTheme;
  readonly dark: SemanticTheme;
}

export class AppearanceStore {
  private value: Appearance;
  private offered: readonly ContributedTheme[] = [];
  private readonly listeners = new Set<() => void>();
  private readonly storage: Storage | null;

  constructor(storage?: Storage | null) {
    this.storage = storage === undefined ? safeStorage() : storage;
    const read = (key: string): unknown => {
      try {
        const text = this.storage?.getItem(key);
        return text ? JSON.parse(text) : null;
      } catch {
        return null;
      }
    };
    const base = read(APPEARANCE_KEY);
    this.value = clean({
      ...(base !== null && typeof base === "object" ? base : {}),
      profiles: read(PROFILE_APPEARANCE_KEY),
    });
  }

  get = (): Appearance => this.value;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** The registry's themes, as last mirrored. */
  contributed = (): readonly ContributedTheme[] => this.offered;

  /** Replace the registry's themes (nothing is stored; a fresh value re-renders the readers). */
  setContributed(list: readonly ContributedTheme[]): void {
    this.offered = list;
    this.value = { ...this.value };
    for (const listener of [...this.listeners]) listener();
  }

  private set(next: Appearance): void {
    this.value = next;
    try {
      const { profiles, ...windowWide } = next;
      this.storage?.setItem(APPEARANCE_KEY, JSON.stringify(windowWide));
      this.storage?.setItem(PROFILE_APPEARANCE_KEY, JSON.stringify(profiles));
    } catch {
      // storage unavailable: the choice lasts for this window
    }
    for (const listener of [...this.listeners]) listener();
  }

  choose(theme: string | null): void {
    this.set({ ...this.value, theme });
  }

  setAccent(hue: number | null): void {
    this.set({ ...this.value, accentHue: hue === null ? null : ((hue % 360) + 360) % 360 });
  }

  addUserTheme(theme: StoredUserTheme): void {
    const others = this.value.userThemes.filter((u) => u.id !== theme.id);
    this.set({ ...this.value, userThemes: [...others, theme] });
  }

  removeUserTheme(id: string): void {
    const theme = this.value.theme === `user:${id}` ? null : this.value.theme;
    this.set({
      ...this.value,
      theme,
      userThemes: this.value.userThemes.filter((u) => u.id !== id),
    });
  }

  setProfile(profile: string, patch: ProfileAppearance): void {
    const merged: Record<string, string> = { ...(this.value.profiles[profile] ?? {}) };
    for (const [key, value] of Object.entries(patch)) {
      if (value === undefined || value === "") delete merged[key];
      else merged[key] = value;
    }
    this.set({ ...this.value, profiles: { ...this.value.profiles, [profile]: merged } });
  }

  /** The theme `profile` paints in `mode`, or null for the built-in look. */
  resolve(profile: string | null, mode: PresetMode): SemanticTheme | null {
    const choice =
      (profile !== null ? this.value.profiles[profile]?.theme : undefined) ?? this.value.theme;
    if (choice === null) return null;
    if (choice.startsWith("contrib:")) {
      const offered = this.offered.find((c) => c.id === choice.slice(8));
      return offered === undefined ? null : offered[mode];
    }
    if (choice.startsWith("user:")) {
      const user = this.value.userThemes.find((u) => u.id === choice.slice(5));
      return user === undefined ? null : fromColours(user);
    }
    if (!presetIds.includes(choice)) return null;
    return presetTheme(
      choice,
      mode,
      this.value.accentHue === null ? {} : { accentHue: this.value.accentHue },
    );
  }

  /** The CSS to paint (colours over the built-in themes, fonts), or "" for the built-in look. */
  css(profile: string | null, mode: PresetMode): string {
    const parts: string[] = [];
    const theme = this.resolve(profile, mode);
    // three :root beat the built-in `:root[data-theme=…]` blocks
    if (theme !== null) parts.push(themeCss(theme, ":root:root:root"));
    const fonts = profile !== null ? this.value.profiles[profile] : undefined;
    const decl: string[] = [];
    if (fonts?.chatFont) decl.push(`  --indus-chat-font: ${fonts.chatFont};`);
    if (fonts?.terminalFont) decl.push(`  --indus-terminal-font: ${fonts.terminalFont};`);
    if (decl.length > 0) parts.push([":root {", ...decl, "}"].join("\n"));
    return parts.join("\n");
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** The window's appearance store. */
export const appearance = new AppearanceStore();

/** Tell the backend which polarity the TUI should use (`tui.theme`). */
export async function syncTui(
  api: RestLike | null,
  profile: string | null,
  mode: PresetMode,
): Promise<boolean> {
  const answer = await rest(api, "PUT", "/api/config", {
    body: { config: { tui: { theme: mode } } },
    ...(profile === null ? {} : { profile }),
  });
  return answer.ok;
}
