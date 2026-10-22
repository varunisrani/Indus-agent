// The shell's remaining contribution consumers (P24-32, RS16): `title-bar` items, `palette-command`
// commands, `keybind` shortcuts and `theme` colour sets. `shell.tsx` holds routes, nav and panes.
//
// - Title bar: the area's items in a strip at the top right of the window band (left of the
//   window controls; on macOS at the right edge), each inside its own error boundary. Nothing is
//   drawn while the area is empty, so the window looks as before until something contributes.
// - Palette: Ctrl/⌘+Shift+P opens a command list — the built-in "go to" entries the shell passes
//   plus every `palette-command` — filtered as you type; Enter runs the selected one.
// - Keybinds: a `keybind` runs when its accelerator is pressed. A plugin never takes a key the app
//   already uses (the pane shortcuts, the rebindable actions' defaults, the palette's own key);
//   such a keybind is ignored. `Mod` means Ctrl, or ⌘ on macOS.
// - Themes: each `theme` contribution's tokens (`#rrggbb` for every semantic name; one set for
//   both sides or `{ light, dark }`) become a choice in Settings › Appearance. A theme that does
//   not check is left out with a warning: the values are colours only, never CSS text.
//
// A contribution's `run` is called in a try/catch: a throwing command or keybind warns and nothing
// else happens.

import { type SemanticName, type SemanticTheme, semanticNames, userTheme } from "@indus/ui";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useT } from "../i18n/index.tsx";
import { actionForKey, isMac, type KeyLike } from "../panes/shortcuts.ts";
import {
  acceleratorFromEvent,
  effectiveBindings,
  normalizeAccelerator,
} from "../settings/keybindings.ts";
import { type AppearanceStore, appearance, type ContributedTheme } from "../theme/themeStore.ts";
import { ContributionArea, useContributionRegistry, useContributions } from "./react.tsx";
import { type Contribution, type ContributionRegistry, contributions } from "./registry.ts";

function warn(id: string, error: unknown): void {
  // biome-ignore lint/suspicious/noConsole: a contribution's failure only warns
  console.warn(`[plugins] ${id} failed`, error);
}

/** Call a contribution's `run`, never letting its failure (sync or async) escape. */
export function runContribution(c: Contribution): void {
  try {
    const result = (c.run as () => unknown)();
    if (result instanceof Promise) result.catch((error: unknown) => warn(c.id, error));
  } catch (error) {
    warn(c.id, error);
  }
}

// -- title bar -----------------------------------------------------------------------------------

/** The `title-bar` items, in a strip of the window band (nothing while the area is empty). */
export function ContributedTitleBar(props: {
  platform: string;
  bandHeight: number;
  registry?: ContributionRegistry;
}) {
  const t = useT();
  const list = useContributions("title-bar", props.registry);
  if (list.length === 0) return null;
  return (
    <div
      className="title-bar-items"
      role="toolbar"
      aria-label={t("titleBar.label")}
      data-platform={isMac(props.platform) ? "mac" : "other"}
      data-band={props.bandHeight > 0 ? "true" : "false"}
      style={props.bandHeight > 0 ? { height: props.bandHeight } : undefined}
    >
      <ContributionArea
        area="title-bar"
        {...(props.registry === undefined ? {} : { registry: props.registry })}
      />
    </div>
  );
}

// -- keybinds ------------------------------------------------------------------------------------

/** The palette's own accelerator. */
export const PALETTE_KEYS = "CommandOrControl+Shift+P";

const fold = (accelerator: string) =>
  accelerator.replace(/\b(Command|Control)\b/g, "CommandOrControl");

/** A contribution's `keys` in canonical form (`Mod` = CommandOrControl), or null. */
export function keysOf(c: Contribution): string | null {
  if (typeof c.keys !== "string") return null;
  const normal = normalizeAccelerator(c.keys.replace(/\bmod\b/gi, "CommandOrControl"));
  return normal === null ? null : fold(normal);
}

/** The accelerators the app keeps for itself (the rebindable actions' defaults, the palette). */
function reservedKeys(): ReadonlySet<string> {
  const out = new Set<string>([PALETTE_KEYS]);
  for (const value of Object.values(effectiveBindings({}))) {
    const normal = normalizeAccelerator(value);
    if (normal !== null) out.add(fold(normal));
  }
  return out;
}

/**
 * The `keybind` contribution `event` runs, or null: none matches, or the key is one the app uses
 * (a pane shortcut, a rebindable action's default, the palette). The first in area order wins.
 */
export function keybindFor(
  list: readonly Contribution[],
  event: KeyLike,
  platform: string,
): Contribution | null {
  const pressed = acceleratorFromEvent(event);
  if (pressed === "") return null;
  if (actionForKey(event, platform) !== null) return null;
  const wanted = fold(pressed);
  if (reservedKeys().has(wanted)) return null;
  return list.find((c) => c.area === "keybind" && keysOf(c) === wanted) ?? null;
}

/** Run the registry's keybinds on this window's keydowns. */
export function useContributedKeybinds(platform: string, registry?: ContributionRegistry): void {
  const provided = useContributionRegistry();
  const source = registry ?? provided;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const found = keybindFor(source.list("keybind"), event, platform);
      if (found === null) return;
      event.preventDefault();
      runContribution(found);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [platform, source]);
}

// -- palette -------------------------------------------------------------------------------------

/** One row of the palette: a built-in entry from the shell, or a `palette-command`. */
export interface PaletteEntry {
  readonly id: string;
  readonly title: string;
  readonly hint?: string;
  run(): void;
}

/** The entries whose title (or hint) holds every word of `query`, in the given order. */
export function filterPalette(entries: readonly PaletteEntry[], query: string): PaletteEntry[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...entries];
  return entries.filter((entry) => {
    const text = `${entry.title} ${entry.hint ?? ""}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

function entryOf(c: Contribution): PaletteEntry {
  return {
    id: c.id,
    title: String(c.title),
    ...(typeof c.hint === "string" && c.hint.length <= 128 ? { hint: c.hint } : {}),
    run: () => runContribution(c),
  };
}

/** Ctrl/⌘+Shift+P: the built-in entries, then the registry's commands. */
export function CommandPalette(props: {
  builtIn: readonly PaletteEntry[];
  registry?: ContributionRegistry;
}) {
  const t = useT();
  const commands = useContributions("palette-command", props.registry);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const input = useRef<HTMLInputElement | null>(null);
  const back = useRef<Element | null>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (fold(acceleratorFromEvent(event)) !== PALETTE_KEYS) return;
      event.preventDefault();
      setOpen((was) => {
        if (!was) back.current = document.activeElement;
        return !was;
      });
      setQuery("");
      setSelected(0);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const entries = useMemo(
    () => filterPalette([...props.builtIn, ...commands.map(entryOf)], query),
    [props.builtIn, commands, query],
  );
  if (!open) return null;

  const close = () => {
    setOpen(false);
    const previous = back.current;
    if (previous instanceof HTMLElement) previous.focus();
  };
  const choose = (entry: PaletteEntry | undefined) => {
    if (entry === undefined) return;
    close();
    entry.run();
  };
  const current = Math.min(selected, Math.max(entries.length - 1, 0));
  const onKey = (event: ReactKeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((current + 1) % Math.max(entries.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((current - 1 + entries.length) % Math.max(entries.length, 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(entries[current]);
    }
  };

  return (
    <div className="command-palette-layer">
      <button
        type="button"
        className="command-palette-backdrop"
        tabIndex={-1}
        aria-label={t("palette.close")}
        onClick={close}
      />
      <div
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label={t("palette.label")}
        onKeyDown={onKey}
      >
        <input
          ref={input}
          role="combobox"
          aria-expanded="true"
          aria-controls="command-palette-list"
          aria-label={t("palette.search")}
          placeholder={t("palette.search")}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setSelected(0);
          }}
        />
        {entries.length === 0 ? (
          <p className="command-palette-empty">{t("palette.empty")}</p>
        ) : (
          <div id="command-palette-list" role="listbox" aria-label={t("palette.label")}>
            {entries.map((entry, index) => (
              <div
                key={entry.id}
                role="option"
                tabIndex={-1}
                aria-selected={index === current}
                className="command-palette-option"
                data-contribution={entry.id}
                onMouseEnter={() => setSelected(index)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  choose(entry);
                }}
              >
                <span>{entry.title}</span>
                {entry.hint === undefined ? null : (
                  <span className="command-palette-hint">{entry.hint}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// -- themes --------------------------------------------------------------------------------------

function isColourSet(value: unknown): value is Readonly<Record<SemanticName, string>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A `theme` contribution as both sides of a theme, or null (with the reason) when it is not one. */
export function themeOf(c: Contribution): ContributedTheme | { error: string } {
  const tokens = c.tokens as Record<string, unknown>;
  const sided = isColourSet(tokens.light) && isColourSet(tokens.dark);
  const light = sided ? tokens.light : tokens;
  const dark = sided ? tokens.dark : tokens;
  if (!isColourSet(light) || !isColourSet(dark)) return { error: "tokens must be colour sets" };
  try {
    const sides = userTheme({ id: c.id, label: String(c.label), light, dark });
    const stamp = (theme: SemanticTheme): SemanticTheme =>
      Object.fromEntries(
        semanticNames.map((name) => [name, { ...theme[name], source: `contrib.${c.id}` }]),
      ) as unknown as SemanticTheme;
    return { id: c.id, label: String(c.label), light: stamp(sides.light), dark: stamp(sides.dark) };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * Keep `store`'s offered themes in step with the `theme` area of `registry`. Returns the stop
 * function (which withdraws every mirrored theme).
 */
export function mirrorThemeContributions(
  registry: ContributionRegistry = contributions,
  store: AppearanceStore = appearance,
): () => void {
  const warned = new Set<Contribution>();
  const sync = () => {
    const out: ContributedTheme[] = [];
    for (const c of registry.list("theme")) {
      const theme = themeOf(c);
      if ("error" in theme) {
        if (!warned.has(c)) warn(c.id, theme.error);
        warned.add(c);
      } else out.push(theme);
    }
    if (out.length > 0 || store.contributed().length > 0) store.setContributed(out);
  };
  sync();
  const off = registry.subscribe(sync);
  return () => {
    off();
    store.setContributed([]);
  };
}
