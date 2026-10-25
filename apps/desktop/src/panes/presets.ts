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

/**
 * The layout a window starts with: the chat alone in both modes (W51-b-3). In Advanced mode the
 * tool panes start on the closed stack, so the band's buttons and "reopen" bring each one back in
 * its usual place; the Default preset still lays them all out on request.
 */
export function initialLayout(mode: InterfaceMode, available: Iterable<PaneId>): PaneLayout {
  const panes = [...available].filter((pane) => paneAllowed(mode, pane));
  const start: PaneLayout = {
    version: 1,
    root: presetTree("focus", panes),
    hidden: [],
    closed: [],
  };
  if (mode === "simple") return start;
  // oldest first: the last one listed is the first "reopen" restores
  const tools = [...TOOL_PANES].reverse().filter((pane) => panes.includes(pane));
  return rememberClosed(start, tools);
}

// -- the right-side group (W52-m-4) --------------------------------------------------------------
//
// Opening a tool pane into the chat-only window (or into a window this arrangement built) lays the
// tool panes out on the right: one column per document pane in a row on top (Preview, Review,
// Files from left to right) and the Terminal under the whole row. A Files column is about 236px
// at the usual window width (a narrow right sidebar); Review gets a wider column so a diff line
// and the commit box fit (W54-D1-3), and Preview one wide enough for a page (W55-F1-3: the band's
// tool cluster now moves off a narrow column whose tab it covered). The chat keeps at least
// about 40% of the span.
// The shapes other presets build are left alone.

/** The top row's panes, left to right. */
const SIDE_TOP: readonly PaneId[] = ["preview", "review", "files"];
/** The chat area's usual width (window minus the sidebar), for the relative split sizes. */
const SIDE_SPAN = 1180;
/** Each side column's usual width at that span (relative sizes, so they scale with the window). */
const SIDE_COLUMN: Readonly<Partial<Record<PaneId, number>>> = {
  preview: 420,
  review: 380,
};
const SIDE_COLUMN_DEFAULT = 236;
/** The chat's least share of the span when several wide columns are open. */
const SIDE_CHAT_MIN = 480;
const SIDE_TERMINAL = 178;
const SIDE_HEIGHT = 846;

/** Whether the right group arranges this pane. */
export function isSidePane(pane: PaneId): boolean {
  return pane === "terminal" || SIDE_TOP.includes(pane);
}

/** The chat with the side panes of `panes` arranged on its right. */
export function sideTree(panes: Iterable<PaneId>): LayoutNode {
  const available = new Set(panes);
  const chat = zone("z1", [CHAT_PANE]);
  const top = pick(available, SIDE_TOP);
  const widthOf = (pane: PaneId) => SIDE_COLUMN[pane] ?? SIDE_COLUMN_DEFAULT;
  const topRow = group(
    "s3",
    "row",
    top.map(
      (pane, index) => [zone(`z${index + 2}`, [pane]), widthOf(pane)] as [LayoutNode, number],
    ),
  );
  const terminal = available.has("terminal") ? zone("z9", ["terminal"]) : null;
  const right = group("s2", "column", [
    [topRow, SIDE_HEIGHT - SIDE_TERMINAL],
    [terminal, SIDE_TERMINAL],
  ]);
  const wanted =
    top.length === 0 ? SIDE_COLUMN_DEFAULT : top.reduce((sum, p) => sum + widthOf(p), 0);
  const width = Math.min(wanted, SIDE_SPAN - SIDE_CHAT_MIN);
  const tree = group("s1", "row", [
    [chat, SIDE_SPAN - width],
    [right, width],
  ]);
  return normalize(tree ?? chat) ?? chat;
}

/** A tree's arrangement without ids and sizes (two trees with the same shape compare equal). */
function shapeOf(node: LayoutNode | null): string {
  if (node === null) return "";
  if (node.kind === "zone") return `[${node.tabs.join(",")}]`;
  return `${node.orientation}(${node.children.map(shapeOf).join(" ")})`;
}

/**
 * Open a side pane into the right group: the new layout, or null when the window holds an
 * arrangement the right group did not build (then the pane opens the usual way).
 */
export function openInSideGroup(layout: PaneLayout, pane: PaneId): PaneLayout | null {
  if (!isSidePane(pane)) return null;
  const open = allPanes(layout.root);
  if (open.includes(pane)) return null;
  if (open.some((p) => p !== CHAT_PANE && !isSidePane(p))) return null;
  if (shapeOf(layout.root) !== shapeOf(sideTree(open))) return null;
  return {
    version: 1,
    root: sideTree([...open, pane]),
    hidden: layout.hidden,
    closed: layout.closed.filter((c) => c.pane !== pane),
  };
}
