// The pane layout as a tree (P20-27). A split lays its children out in a row or a column with
// relative sizes; a zone stacks panes as tabs with one active. Every operation here is pure: it
// takes a layout and returns a new one in canonical form (no empty zones, no single-child splits,
// no split nested in a split of the same direction), and a pane is never in two places.
//
// Besides the tree a layout carries the panes that are hidden (stateful panes kept mounted but
// inert) and a bounded stack of closed panes with where they were, so "reopen last closed" and
// presets can put them back.

export type PaneId = string;
export type Orientation = "row" | "column";
export type DropEdge = "center" | "left" | "right" | "top" | "bottom";

export interface ZoneNode {
  readonly kind: "zone";
  readonly id: string;
  readonly tabs: readonly PaneId[];
  readonly active: PaneId;
  readonly minimized: boolean;
}

export interface SplitNode {
  readonly kind: "split";
  readonly id: string;
  readonly orientation: Orientation;
  readonly children: readonly LayoutNode[];
  /** Relative sizes, one per child (positive). */
  readonly sizes: readonly number[];
}

export type LayoutNode = ZoneNode | SplitNode;

export interface ClosedPane {
  readonly pane: PaneId;
  /** The zone it was in (may be gone by now). */
  readonly zoneId: string | null;
  /** A pane that shared its zone, to find the place again when the zone is gone. */
  readonly neighbour: PaneId | null;
}

export interface PaneLayout {
  readonly version: 1;
  readonly root: LayoutNode | null;
  readonly hidden: readonly PaneId[];
  readonly closed: readonly ClosedPane[];
}

export interface DropTarget {
  readonly zoneId: string;
  readonly edge: DropEdge;
  /** For a centre drop: the tab position (default: after the last tab). */
  readonly index?: number;
}

/** The pane that holds the route's conversation: always present, never closed. */
export const CHAT_PANE: PaneId = "chat";
export const MAX_CLOSED = 20;
const MIN_SIZE = 0.05;

// -- building ------------------------------------------------------------------------------------

export function emptyLayout(): PaneLayout {
  return { version: 1, root: null, hidden: [], closed: [] };
}

export function zone(id: string, tabs: readonly PaneId[], active?: PaneId): ZoneNode {
  return { kind: "zone", id, tabs: [...tabs], active: active ?? tabs[0] ?? "", minimized: false };
}

export function split(
  id: string,
  orientation: Orientation,
  children: readonly LayoutNode[],
  sizes?: readonly number[],
): SplitNode {
  return {
    kind: "split",
    id,
    orientation,
    children: [...children],
    sizes: sizes === undefined ? children.map(() => 1) : [...sizes],
  };
}

export function layoutOf(root: LayoutNode | null): PaneLayout {
  return { version: 1, root: root === null ? null : normalize(root), hidden: [], closed: [] };
}

// -- queries -------------------------------------------------------------------------------------

export function allPanes(node: LayoutNode | null): PaneId[] {
  if (node === null) return [];
  return node.kind === "zone" ? [...node.tabs] : node.children.flatMap((child) => allPanes(child));
}

export function zones(node: LayoutNode | null): ZoneNode[] {
  if (node === null) return [];
  return node.kind === "zone" ? [node] : node.children.flatMap((child) => zones(child));
}

export function findZone(node: LayoutNode | null, zoneId: string): ZoneNode | null {
  return zones(node).find((z) => z.id === zoneId) ?? null;
}

export function zoneOf(node: LayoutNode | null, pane: PaneId): ZoneNode | null {
  return zones(node).find((z) => z.tabs.includes(pane)) ?? null;
}

export function hasPane(layout: PaneLayout, pane: PaneId): boolean {
  return zoneOf(layout.root, pane) !== null;
}

function allIds(node: LayoutNode | null): string[] {
  if (node === null) return [];
  return node.kind === "zone" ? [node.id] : [node.id, ...node.children.flatMap(allIds)];
}

/** A fresh id `z<n>` / `s<n>` not used in the tree (deterministic). */
export function freshId(node: LayoutNode | null, prefix: "z" | "s"): string {
  const used = new Set(allIds(node));
  let n = 1;
  while (used.has(`${prefix}${n}`)) n += 1;
  return `${prefix}${n}`;
}

// -- canonical form -------------------------------------------------------------------------------

/** Drop empty zones, unwrap single-child splits, flatten same-direction nesting; fix `active`. */
export function normalize(node: LayoutNode): LayoutNode | null {
  if (node.kind === "zone") {
    const seen = new Set<PaneId>();
    const tabs = node.tabs.filter((pane) => {
      if (pane === "" || seen.has(pane)) return false;
      seen.add(pane);
      return true;
    });
    if (tabs.length === 0) return null;
    const active = tabs.includes(node.active) ? node.active : (tabs[0] as PaneId);
    if (tabs.length === node.tabs.length && active === node.active) return node;
    return { ...node, tabs, active };
  }
  const children: LayoutNode[] = [];
  const sizes: number[] = [];
  node.children.forEach((child, index) => {
    const kept = normalize(child);
    if (kept === null) return;
    const weight = positive(node.sizes[index]);
    if (kept.kind === "split" && kept.orientation === node.orientation) {
      const total = kept.sizes.reduce((sum, size) => sum + positive(size), 0) || 1;
      kept.children.forEach((grandchild, j) => {
        children.push(grandchild);
        sizes.push((weight * positive(kept.sizes[j])) / total);
      });
      return;
    }
    children.push(kept);
    sizes.push(weight);
  });
  if (children.length === 0) return null;
  if (children.length === 1) return children[0] as LayoutNode;
  return { ...node, children, sizes };
}

function positive(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 1;
}

function withRoot(layout: PaneLayout, root: LayoutNode | null): PaneLayout {
  const next = root === null ? null : normalize(root);
  const present = new Set(allPanes(next));
  return {
    ...layout,
    root: next,
    hidden: layout.hidden.filter((pane) => present.has(pane)),
  };
}

/** Post-order map: children first, then the node itself (a replacement is not walked again). */
function mapNode(node: LayoutNode, fn: (node: LayoutNode) => LayoutNode): LayoutNode {
  if (node.kind === "zone") return fn(node);
  return fn({ ...node, children: node.children.map((child) => mapNode(child, fn)) });
}

// -- structural edits ----------------------------------------------------------------------------

/** Take a pane out of the tree (no closed record). */
export function removePane(layout: PaneLayout, pane: PaneId): PaneLayout {
  if (layout.root === null || !hasPane(layout, pane)) return layout;
  const root = mapNode(layout.root, (node) =>
    node.kind === "zone" && node.tabs.includes(pane)
      ? {
          ...node,
          tabs: node.tabs.filter((tab) => tab !== pane),
          active: node.active === pane ? neighbourTab(node, pane) : node.active,
        }
      : node,
  );
  return withRoot(layout, root);
}

function neighbourTab(z: ZoneNode, pane: PaneId): PaneId {
  const index = z.tabs.indexOf(pane);
  const rest = z.tabs.filter((tab) => tab !== pane);
  return rest[Math.min(index, rest.length - 1)] ?? "";
}

/** Put a pane at `target` (a pane already in the tree moves there). */
export function insertPane(layout: PaneLayout, pane: PaneId, target?: DropTarget): PaneLayout {
  const base = hasPane(layout, pane) ? removePane(layout, pane) : layout;
  const opened = { ...base, closed: base.closed.filter((c) => c.pane !== pane) };
  if (opened.root === null) return withRoot(opened, zone("z1", [pane]));
  const where = target ?? defaultTarget(opened.root);
  const host = findZone(opened.root, where.zoneId);
  if (host === null) return insertPane(opened, pane, defaultTarget(opened.root));
  if (where.edge === "center") {
    const root = mapNode(opened.root, (node) => {
      if (node.kind !== "zone" || node.id !== host.id) return node;
      const tabs = [...node.tabs];
      const at = clampIndex(where.index, tabs.length);
      tabs.splice(at, 0, pane);
      return { ...node, tabs, active: pane, minimized: false };
    });
    return withRoot(opened, root);
  }
  const fresh = zone(freshId(opened.root, "z"), [pane]);
  const orientation: Orientation =
    where.edge === "left" || where.edge === "right" ? "row" : "column";
  const before = where.edge === "left" || where.edge === "top";
  const splitId = freshId(opened.root, "s");
  const root = mapNode(opened.root, (node) =>
    node.kind === "zone" && node.id === host.id
      ? split(splitId, orientation, before ? [fresh, node] : [node, fresh])
      : node,
  );
  return withRoot(opened, root);
}

function clampIndex(index: number | undefined, length: number): number {
  if (index === undefined || !Number.isFinite(index)) return length;
  return Math.max(0, Math.min(length, Math.trunc(index)));
}

/** Where a pane goes when nothing says: the last zone without the chat, else beside the chat. */
export function defaultTarget(root: LayoutNode): DropTarget {
  const all = zones(root);
  const tool = [...all].reverse().find((z) => !z.tabs.includes(CHAT_PANE));
  if (tool !== undefined) return { zoneId: tool.id, edge: "center" };
  const last = all[all.length - 1] as ZoneNode;
  return { zoneId: last.id, edge: "right" };
}

/** Drag a pane onto a zone's centre (join its tabs) or an edge (split it). */
export function movePane(layout: PaneLayout, pane: PaneId, target: DropTarget): PaneLayout {
  const source = zoneOf(layout.root, pane);
  if (source === null) return layout;
  if (source.id === target.zoneId) {
    if (target.edge === "center") return reorderTab(layout, pane, target.index);
    if (source.tabs.length === 1) return layout; // splitting a lone zone with itself
  }
  if (findZone(layout.root, target.zoneId) === null) return layout;
  // Removing the pane may dissolve its zone; the target zone keeps its id either way.
  return insertPane(removePane(layout, pane), pane, target);
}

function reorderTab(layout: PaneLayout, pane: PaneId, index: number | undefined): PaneLayout {
  if (layout.root === null) return layout;
  const root = mapNode(layout.root, (node) => {
    if (node.kind !== "zone" || !node.tabs.includes(pane)) return node;
    const without = node.tabs.filter((tab) => tab !== pane);
    const from = node.tabs.indexOf(pane);
    let at = clampIndex(index, node.tabs.length);
    if (at > from) at -= 1;
    without.splice(Math.min(at, without.length), 0, pane);
    return { ...node, tabs: without, active: pane };
  });
  return withRoot(layout, root);
}

/** Close a pane (never the chat): out of the tree, onto the closed stack. */
export function closePane(layout: PaneLayout, pane: PaneId): PaneLayout {
  if (pane === CHAT_PANE) return layout;
  const source = zoneOf(layout.root, pane);
  if (source === null) return layout;
  const neighbour = source.tabs.find((tab) => tab !== pane) ?? siblingPane(layout.root, source);
  const removed = removePane(layout, pane);
  const record: ClosedPane = { pane, zoneId: source.id, neighbour };
  const closed = [...removed.closed.filter((c) => c.pane !== pane), record].slice(-MAX_CLOSED);
  return { ...removed, closed };
}

function siblingPane(root: LayoutNode | null, of: ZoneNode): PaneId | null {
  const all = zones(root);
  const index = all.findIndex((z) => z.id === of.id);
  const near = all[index + 1] ?? all[index - 1];
  return near?.active ?? null;
}

/** Remember panes as closed without touching the tree (presets that leave them out). */
export function rememberClosed(layout: PaneLayout, panes: readonly PaneId[]): PaneLayout {
  const present = new Set(allPanes(layout.root));
  const fresh = panes.filter((pane) => pane !== CHAT_PANE && !present.has(pane));
  if (fresh.length === 0) return layout;
  const kept = layout.closed.filter((c) => !fresh.includes(c.pane));
  const added = fresh.map((pane) => ({ pane, zoneId: null, neighbour: null }));
  return { ...layout, closed: [...kept, ...added].slice(-MAX_CLOSED) };
}

/** Put the most recently closed pane back where it was (`allowed` filters what may come back). */
export function reopenLastClosed(
  layout: PaneLayout,
  allowed: (pane: PaneId) => boolean = () => true,
): { layout: PaneLayout; pane: PaneId | null } {
  for (let i = layout.closed.length - 1; i >= 0; i -= 1) {
    const record = layout.closed[i] as ClosedPane;
    if (!allowed(record.pane) || hasPane(layout, record.pane)) continue;
    const rest = { ...layout, closed: layout.closed.filter((_, j) => j !== i) };
    return { layout: reopenPane(rest, record), pane: record.pane };
  }
  return { layout, pane: null };
}

/** Reopen a specific pane (from the closed stack if it is there, else at the default place). */
export function openPane(layout: PaneLayout, pane: PaneId): PaneLayout {
  if (hasPane(layout, pane)) return activate(layout, pane);
  const record = layout.closed.find((c) => c.pane === pane) ?? {
    pane,
    zoneId: null,
    neighbour: null,
  };
  return reopenPane({ ...layout, closed: layout.closed.filter((c) => c.pane !== pane) }, record);
}

function reopenPane(layout: PaneLayout, record: ClosedPane): PaneLayout {
  if (record.zoneId !== null && findZone(layout.root, record.zoneId) !== null) {
    return insertPane(layout, record.pane, { zoneId: record.zoneId, edge: "center" });
  }
  const near = record.neighbour === null ? null : zoneOf(layout.root, record.neighbour);
  if (near !== null) return insertPane(layout, record.pane, { zoneId: near.id, edge: "center" });
  return insertPane(layout, record.pane);
}

export function activate(layout: PaneLayout, pane: PaneId): PaneLayout {
  if (layout.root === null || !hasPane(layout, pane)) return layout;
  const root = mapNode(layout.root, (node) =>
    node.kind === "zone" && node.tabs.includes(pane)
      ? { ...node, active: pane, minimized: false }
      : node,
  );
  return { ...withRoot(layout, root), hidden: layout.hidden.filter((h) => h !== pane) };
}

export function setMinimized(layout: PaneLayout, zoneId: string, minimized: boolean): PaneLayout {
  if (layout.root === null || findZone(layout.root, zoneId) === null) return layout;
  const root = mapNode(layout.root, (node) =>
    node.kind === "zone" && node.id === zoneId ? { ...node, minimized } : node,
  );
  return withRoot(layout, root);
}

/** Hide (keep mounted, inert) or show a pane. */
export function setHidden(layout: PaneLayout, pane: PaneId, hidden: boolean): PaneLayout {
  if (!hasPane(layout, pane) || pane === CHAT_PANE) return layout;
  const rest = layout.hidden.filter((h) => h !== pane);
  return { ...layout, hidden: hidden ? [...rest, pane] : rest };
}

export function isHidden(layout: PaneLayout, pane: PaneId): boolean {
  return layout.hidden.includes(pane);
}

/** The next (+1) or previous (-1) tab of a zone. */
export function cycleTab(layout: PaneLayout, zoneId: string, step: 1 | -1): PaneLayout {
  const host = findZone(layout.root, zoneId);
  if (host === null || host.tabs.length < 2) return layout;
  const index = host.tabs.indexOf(host.active);
  const next = host.tabs[(index + step + host.tabs.length) % host.tabs.length] as PaneId;
  return activate(layout, next);
}

/** Tab `slot` (1-based) of a zone; 9 is always the last tab. */
export function jumpToSlot(layout: PaneLayout, zoneId: string, slot: number): PaneLayout {
  const host = findZone(layout.root, zoneId);
  if (host === null || slot < 1) return layout;
  const pane = slot >= 9 ? host.tabs[host.tabs.length - 1] : host.tabs[slot - 1];
  return pane === undefined ? layout : activate(layout, pane);
}

export function resizeSplit(
  layout: PaneLayout,
  splitId: string,
  sizes: readonly number[],
): PaneLayout {
  if (layout.root === null) return layout;
  const root = mapNode(layout.root, (node) => {
    if (node.kind !== "split" || node.id !== splitId || sizes.length !== node.children.length) {
      return node;
    }
    const total = sizes.reduce((sum, size) => sum + Math.max(0, size), 0) || 1;
    const clamped = sizes.map((size) => Math.max(MIN_SIZE, Math.max(0, size) / total));
    return { ...node, sizes: clamped };
  });
  return withRoot(layout, root);
}

/** Keep only panes `keep` accepts; the others go on the closed stack. */
export function retainPanes(layout: PaneLayout, keep: (pane: PaneId) => boolean): PaneLayout {
  let next = layout;
  for (const pane of allPanes(layout.root)) {
    if (!keep(pane) && pane !== CHAT_PANE) next = closePane(next, pane);
  }
  return next;
}

// -- persistence ---------------------------------------------------------------------------------

function parseNode(raw: unknown, depth: number): LayoutNode | null {
  if (depth > 16 || typeof raw !== "object" || raw === null) return null;
  const value = raw as Record<string, unknown>;
  const id = typeof value.id === "string" && value.id !== "" ? value.id : null;
  if (id === null) return null;
  if (value.kind === "zone") {
    const tabs = Array.isArray(value.tabs)
      ? value.tabs.filter((tab): tab is string => typeof tab === "string" && tab !== "")
      : [];
    return {
      kind: "zone",
      id,
      tabs,
      active: typeof value.active === "string" ? value.active : (tabs[0] ?? ""),
      minimized: value.minimized === true,
    };
  }
  if (value.kind === "split") {
    const orientation = value.orientation === "column" ? "column" : "row";
    const children = Array.isArray(value.children)
      ? value.children
          .map((child) => parseNode(child, depth + 1))
          .filter((child): child is LayoutNode => child !== null)
      : [];
    const rawSizes = Array.isArray(value.sizes) ? value.sizes : [];
    const sizes = children.map((_, index) => positive(rawSizes[index] as number | undefined));
    return { kind: "split", id, orientation, children, sizes };
  }
  return null;
}

/** Drop later copies of a pane and repeated ids so a hand-edited or old layout stays sound. */
function dedupe(node: LayoutNode, seenPanes: Set<PaneId>, seenIds: Set<string>): LayoutNode {
  const id = seenIds.has(node.id) ? `${node.id}-${seenIds.size}` : node.id;
  seenIds.add(id);
  if (node.kind === "zone") {
    const tabs = node.tabs.filter((pane) => {
      if (seenPanes.has(pane)) return false;
      seenPanes.add(pane);
      return true;
    });
    return { ...node, id, tabs };
  }
  return { ...node, id, children: node.children.map((child) => dedupe(child, seenPanes, seenIds)) };
}

/** A stored layout (any JSON), repaired; null when it is not a layout at all. */
export function parseLayout(raw: unknown): PaneLayout | null {
  if (typeof raw !== "object" || raw === null) return null;
  const value = raw as Record<string, unknown>;
  if (value.version !== 1) return null;
  const parsed = value.root === null ? null : parseNode(value.root, 0);
  const root = parsed === null ? null : normalize(dedupe(parsed, new Set(), new Set()));
  const present = new Set(allPanes(root));
  const hidden = Array.isArray(value.hidden)
    ? value.hidden.filter((pane): pane is string => typeof pane === "string" && present.has(pane))
    : [];
  const closed: ClosedPane[] = [];
  if (Array.isArray(value.closed)) {
    for (const entry of value.closed) {
      if (typeof entry !== "object" || entry === null) continue;
      const record = entry as Record<string, unknown>;
      if (typeof record.pane !== "string" || present.has(record.pane)) continue;
      if (closed.some((c) => c.pane === record.pane)) continue;
      closed.push({
        pane: record.pane,
        zoneId: typeof record.zoneId === "string" ? record.zoneId : null,
        neighbour: typeof record.neighbour === "string" ? record.neighbour : null,
      });
    }
  }
  return { version: 1, root, hidden: [...new Set(hidden)], closed: closed.slice(-MAX_CLOSED) };
}

/** Problems with a layout (empty when sound): used by tests and the store's debug check. */
export function layoutProblems(layout: PaneLayout): string[] {
  const problems: string[] = [];
  const panes = allPanes(layout.root);
  const unique = new Set(panes);
  if (unique.size !== panes.length) problems.push("a pane appears twice");
  for (const record of layout.closed) {
    if (unique.has(record.pane)) problems.push(`closed pane ${record.pane} is also open`);
  }
  for (const pane of layout.hidden) {
    if (!unique.has(pane)) problems.push(`hidden pane ${pane} is not open`);
  }
  const ids = [...zones(layout.root).map((z) => z.id), ...splitIds(layout.root)];
  if (new Set(ids).size !== ids.length) problems.push("a node id appears twice");
  const check = (node: LayoutNode | null): void => {
    if (node === null) return;
    if (node.kind === "zone") {
      if (node.tabs.length === 0) problems.push(`zone ${node.id} is empty`);
      if (!node.tabs.includes(node.active)) problems.push(`zone ${node.id} has no active tab`);
      return;
    }
    if (node.children.length < 2) problems.push(`split ${node.id} has fewer than two children`);
    if (node.sizes.length !== node.children.length) problems.push(`split ${node.id} sizes`);
    for (const child of node.children) {
      if (child.kind === "split" && child.orientation === node.orientation) {
        problems.push(`split ${child.id} repeats its parent's direction`);
      }
      check(child);
    }
  };
  check(layout.root);
  return problems;
}

function splitIds(node: LayoutNode | null): string[] {
  if (node === null || node.kind === "zone") return [];
  return [node.id, ...node.children.flatMap(splitIds)];
}
