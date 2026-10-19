/**
 * Detail visibility: one global mode (uiStore `detailsMode`) plus per-section overrides.
 *
 * Resolution for a section: its explicit override → the global mode when the user set it with
 * `/details <mode>` in this session → the built-in section default → the global mode.
 */
import { createStore, type StoreApi } from "zustand/vanilla";
import { uiStore } from "../state/uiStore.ts";

export const DETAIL_MODES = ["hidden", "collapsed", "expanded"] as const;
export type DetailMode = (typeof DETAIL_MODES)[number];

export const SECTIONS = ["thinking", "tools", "subagents", "activity"] as const;
export type Section = (typeof SECTIONS)[number];

export type SectionOverrides = Partial<Record<Section, DetailMode>>;

/** Out-of-the-box section modes; `subagents` has none and follows the global mode. */
export const SECTION_DEFAULTS: SectionOverrides = {
  thinking: "expanded",
  tools: "expanded",
  activity: "hidden",
};

export function asMode(value: unknown): DetailMode | null {
  const text = String(value ?? "")
    .trim()
    .toLowerCase();
  return (DETAIL_MODES as readonly string[]).includes(text) ? (text as DetailMode) : null;
}

export function asSection(value: unknown): Section | null {
  const text = String(value ?? "")
    .trim()
    .toLowerCase();
  return (SECTIONS as readonly string[]).includes(text) ? (text as Section) : null;
}

/** Keeps only known sections with valid modes (config blobs may hold anything). */
export function readOverrides(raw: unknown): SectionOverrides {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: SectionOverrides = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const section = asSection(key);
    const mode = asMode(value);
    if (section && mode) out[section] = mode;
  }
  return out;
}

export function sectionMode(
  section: Section,
  global: DetailMode,
  overrides: SectionOverrides = {},
  globalFromCommand = false,
): DetailMode {
  return overrides[section] ?? (globalFromCommand ? global : (SECTION_DEFAULTS[section] ?? global));
}

export interface ResolvedSections {
  thinking: DetailMode;
  tools: DetailMode;
  subagents: DetailMode;
  activity: DetailMode;
}

export function resolveSections(
  global: DetailMode,
  overrides: SectionOverrides = {},
  globalFromCommand = false,
): ResolvedSections {
  return {
    thinking: sectionMode("thinking", global, overrides, globalFromCommand),
    tools: sectionMode("tools", global, overrides, globalFromCommand),
    subagents: sectionMode("subagents", global, overrides, globalFromCommand),
    activity: sectionMode("activity", global, overrides, globalFromCommand),
  };
}

export function allHidden(sections: ResolvedSections): boolean {
  return SECTIONS.every((name) => sections[name] === "hidden");
}

export function nextMode(mode: DetailMode): DetailMode {
  return DETAIL_MODES[(DETAIL_MODES.indexOf(mode) + 1) % DETAIL_MODES.length] as DetailMode;
}

// ---------------------------------------------------------------- `/details`

export interface DetailsState {
  global: DetailMode;
  overrides: SectionOverrides;
  /** The global mode was set by `/details` this session, so it beats the section defaults. */
  globalFromCommand: boolean;
}

export type DetailsResult =
  | { ok: true; state: DetailsState; message: string }
  | { ok: false; message: string };

const USAGE =
  "usage: /details [hidden|collapsed|expanded|cycle] or /details <section> <mode|reset>";

/**
 * Applies `/details` arguments to a state. No arguments reports the current modes; a mode or
 * `cycle` sets the global mode; `<section> <mode|reset>` sets or clears one section.
 */
export function detailsCommand(state: DetailsState, args: string): DetailsResult {
  const words = args.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    const sections = resolveSections(state.global, state.overrides, state.globalFromCommand);
    const parts = SECTIONS.map((name) => `${name} ${sections[name]}`).join(", ");
    return { ok: true, state, message: `details: ${state.global} (${parts})` };
  }
  const [first, second, ...extra] = words;
  if (words.length === 1) {
    const mode = first?.toLowerCase() === "cycle" ? nextMode(state.global) : asMode(first);
    if (!mode) return { ok: false, message: `unknown details mode "${first}"; ${USAGE}` };
    return {
      ok: true,
      state: { ...state, global: mode, globalFromCommand: true },
      message: `details: ${mode} for every section`,
    };
  }
  const section = asSection(first);
  if (!section || extra.length > 0)
    return { ok: false, message: `unknown details section "${first}"; ${USAGE}` };
  const overrides = { ...state.overrides };
  if (second?.toLowerCase() === "reset") {
    delete overrides[section];
    return {
      ok: true,
      state: { ...state, overrides },
      message: `details: ${section} follows the default again`,
    };
  }
  const current = sectionMode(section, state.global, state.overrides, state.globalFromCommand);
  const mode = second?.toLowerCase() === "cycle" ? nextMode(current) : asMode(second);
  if (!mode) return { ok: false, message: `unknown details mode "${second}"; ${USAGE}` };
  overrides[section] = mode;
  return { ok: true, state: { ...state, overrides }, message: `details: ${section} ${mode}` };
}

// ---------------------------------------------------------------- store

export interface DetailsStore {
  overrides: SectionOverrides;
  globalFromCommand: boolean;
  set(p: Partial<Pick<DetailsStore, "overrides" | "globalFromCommand">>): void;
}

/** Section overrides; the global mode itself stays in uiStore. */
export const detailsStore: StoreApi<DetailsStore> = createStore<DetailsStore>((set) => ({
  overrides: {},
  globalFromCommand: false,
  set: (p) => set(p),
}));

/** Runs `/details <args>` against the live stores and returns the line to show. */
export function applyDetailsCommand(args: string): DetailsResult {
  const { overrides, globalFromCommand } = detailsStore.getState();
  const result = detailsCommand(
    { global: uiStore.getState().detailsMode, overrides, globalFromCommand },
    args,
  );
  if (result.ok) {
    detailsStore.getState().set({
      overrides: result.state.overrides,
      globalFromCommand: result.state.globalFromCommand,
    });
    if (result.state.global !== uiStore.getState().detailsMode)
      uiStore.getState().patch({ detailsMode: result.state.global });
  }
  return result;
}
