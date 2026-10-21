// Layout presets and interface modes (P20-27).
//
// A preset rebuilds the tree from the panes that exist in this window; the panes it leaves out
// go on the closed stack, so a saved layout still remembers them. Interface modes: Advanced (the
// default) shows everything; Simple is chat-first and hides the status bar, the profile rail when
// there is only one profile, the terminal / files / review panes, the technical tool view and
// inline diffs, and keeps thinking collapsed. Each mode keeps its own arrangement (layoutStore).

import {
  allPanes,
  CHAT_PANE,
  type LayoutNode,
  normalize,
  type PaneId,
  type PaneLayout,
  rememberClosed,
  split,
  zone,
} from "./paneTree.ts";

export const PRESET_IDS = ["basic", "focus", "default", "terminal-deck", "quad"] as const;
export type PresetId = (typeof PRESET_IDS)[number];

export const INTERFACE_MODES = ["advanced", "simple"] as const;
export type InterfaceMode = (typeof INTERFACE_MODES)[number];
export const DEFAULT_MODE: InterfaceMode = "advanced";

/** The tool panes the desktop knows (P20-28..31 register their bodies). */
export const TOOL_PANES = ["preview", "files", "review", "terminal"] as const;

/** Panes Simple mode never shows. */
export const SIMPLE_HIDDEN_PANES: ReadonlySet<PaneId> = new Set(["terminal", "files", "review"]);

/** Shell surfaces that an interface mode can hide. */
export type Surface =
  | "statusBar"
  | "profileRail"
  | "toolDetails"
  | "inlineDiffs"
  | "thinkingExpanded";

export interface SurfaceContext {
  /** How many profiles exist (the rail stays in Simple mode when there are several). */
  readonly profileCount: number;
}

export function surfaceShown(mode: InterfaceMode, surface: Surface, ctx: SurfaceContext): boolean {
  if (mode === "advanced") return true;
  if (surface === "profileRail") return ctx.profileCount > 1;
  return false;
}

export function paneAllowed(mode: InterfaceMode, pane: PaneId): boolean {
  return mode === "advanced" || !SIMPLE_HIDDEN_PANES.has(pane);
}

/** The tool panes of a list, in the desktop's usual order. */
function pick(available: ReadonlySet<PaneId>, panes: readonly PaneId[]): PaneId[] {
  return panes.filter((pane) => available.has(pane));
}

/** Build a group: one zone, or nothing when none of its panes exist. */
function maybeZone(id: string, panes: readonly PaneId[]): LayoutNode | null {
  return panes.length === 0 ? null : zone(id, panes);
}

/** A split of the parts that exist (a lone part stands alone; none at all → null). */
function group(
  id: string,
  orientation: "row" | "column",
  parts: ReadonlyArray<[LayoutNode | null, number]>,
): LayoutNode | null {
  const kept = parts.filter((part): part is [LayoutNode, number] => part[0] !== null);
  if (kept.length === 0) return null;
  if (kept.length === 1) return (kept[0] as [LayoutNode, number])[0];
  return split(
    id,
    orientation,
    kept.map(([node]) => node),
    kept.map(([, size]) => size),
  );
}

/** The tree of a preset over the panes this window has (the chat is always there). */
export function presetTree(id: PresetId, panes: Iterable<PaneId>): LayoutNode {
  const available = new Set(panes);
  const chat = zone("z1", [CHAT_PANE]);
  const docs = pick(available, ["preview", "files", "review"]);
  const terminal = pick(available, ["terminal"]);
  let tree: LayoutNode | null;
  switch (id) {
    case "focus":
      tree = chat;
      break;
    case "basic":
      tree = group("s1", "row", [
        [chat, 3],
        [maybeZone("z2", [...docs, ...terminal]), 2],
      ]);
      break;
    case "default":
      tree = group("s1", "row", [
        [chat, 3],
        [
          group("s2", "column", [
            [maybeZone("z2", docs), 2],
            [maybeZone("z3", terminal), 1],
          ]),
          2,
        ],
      ]);
      break;
    case "terminal-deck":
      tree = group("s1", "column", [
        [
          group("s2", "row", [
            [chat, 3],
            [maybeZone("z2", docs), 2],
          ]),
          2,
        ],
        [maybeZone("z3", terminal), 1],
      ]);
      break;
    case "quad":
      tree = group("s1", "column", [
        [
          group("s2", "row", [
            [chat, 1],
            [maybeZone("z2", pick(available, ["preview"])), 1],
          ]),
          1,
        ],
        [
          group("s3", "row", [
            [maybeZone("z3", pick(available, ["files", "review"])), 1],
            [maybeZone("z4", terminal), 1],
          ]),
          1,
        ],
      ]);
      break;
  }
  return normalize(tree ?? chat) ?? chat;
}

/** Apply a preset: the new tree, with every pane it leaves out remembered as closed. */
export function applyPreset(
  id: PresetId,
  current: PaneLayout,
  available: Iterable<PaneId>,
): PaneLayout {
  const panes = [...available];
  const root = presetTree(id, panes);
  const placed = new Set(allPanes(root));
  const before = new Set([...allPanes(current.root), ...current.closed.map((c) => c.pane)]);
  const leftOut = [...before].filter((pane) => !placed.has(pane) && panes.includes(pane));
  const fresh: PaneLayout = {
    version: 1,
    root,
    hidden: current.hidden.filter((pane) => placed.has(pane)),
    closed: current.closed.filter((c) => !placed.has(c.pane)),
  };
  return rememberClosed(fresh, leftOut);
}

/** The layout a window starts with: Default in Advanced mode, Focus in Simple mode. */
export function initialLayout(mode: InterfaceMode, available: Iterable<PaneId>): PaneLayout {
  const panes = [...available].filter((pane) => paneAllowed(mode, pane));
  const preset: PresetId = mode === "simple" ? "focus" : "default";
  return { version: 1, root: presetTree(preset, panes), hidden: [], closed: [] };
}
