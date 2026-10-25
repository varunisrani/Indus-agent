// The pane shell (P20-27): renders the layout tree of `layoutStore.ts`. Zones show their panes as
// tabs; tabs drag onto another zone's centre (join) or edge (split). A stateful pane (preview,
// terminal) is never unmounted while it is open: Hide keeps its body in the DOM, `hidden` and
// `inert`; Close releases it. Zones without stateful panes (and without the chat) minimise to the
// restore rail. Unfocused zones are desaturated by the stylesheet (`data-focused`).
//
// Tool panes register here (P20-28..31): `paneRegistry.register({ id, stateful, render })`.

import {
  EyeOff,
  FolderTree,
  GitCompareArrows,
  Globe,
  LayoutDashboard,
  LayoutGrid,
  LayoutTemplate,
  Minus,
  PanelRight,
  RectangleHorizontal,
  SquareTerminal,
  X,
} from "lucide-react";
import {
  createContext,
  type DragEvent,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { ChatConnection } from "../gateway/connectionStore.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { useDisplayPrefs } from "../settings/displayPrefs.ts";
import { LayoutEditor } from "./LayoutEditor.tsx";
import { type LayoutStore, withoutUnknown } from "./layoutStore.ts";
import {
  allPanes,
  CHAT_PANE,
  type DropEdge,
  isHidden,
  type LayoutNode,
  type PaneId,
  type PaneLayout,
  type SplitNode,
  type ZoneNode,
  zones,
} from "./paneTree.ts";
import {
  INTERFACE_MODES,
  type InterfaceMode,
  PRESET_IDS,
  type PresetId,
  paneAllowed,
} from "./presets.ts";
import type { PreviewTarget } from "./preview/target.ts";
import "./panes.css";

// -- the registry --------------------------------------------------------------------------------

export interface PaneRenderContext {
  /** The route's conversation (null: a new chat). */
  readonly sessionId: string | null;
  readonly profile: string | null;
  /** The pane is the active tab of a visible zone. */
  readonly active: boolean;
  /** Hidden (kept mounted, inert) or behind another tab. */
  readonly hidden: boolean;
  /** The conversation's working folder (a local path, or the remote host's path), if any. */
  readonly workspace: string | null;
  /** Where the foreground connection runs (null while unknown). */
  readonly mode: "local" | "remote" | null;
  readonly connection: ChatConnection | null;
  /** Show something in the preview pane (opening and focusing it). */
  openPreview?(target: PreviewTarget): void;
  /** Put text into the composer of the route's conversation. */
  addToChat?(text: string): void;
  /** Close this pane (a pane that draws its own header puts its close button there). */
  close?(): void;
}

export interface PaneDefinition {
  readonly id: PaneId;
  /** Keeps state that must survive hiding (a webview, a shell): Hide, never unmount. */
  readonly stateful: boolean;
  /** Shown on the tab; known ids have built-in titles. */
  readonly title?: string;
  render(context: PaneRenderContext): ReactNode;
}

export class PaneRegistry {
  private readonly panes = new Map<PaneId, PaneDefinition>();
  private readonly listeners = new Set<() => void>();
  private version = 0;

  register(definition: PaneDefinition): () => void {
    this.panes.set(definition.id, definition);
    this.bump();
    return () => {
      if (this.panes.get(definition.id) === definition) {
        this.panes.delete(definition.id);
        this.bump();
      }
    };
  }

  get(id: PaneId): PaneDefinition | undefined {
    return this.panes.get(id);
  }

  /** Registered ids, the chat first. */
  ids(): PaneId[] {
    return [CHAT_PANE, ...[...this.panes.keys()].filter((id) => id !== CHAT_PANE)];
  }

  isStateful(id: PaneId): boolean {
    return this.panes.get(id)?.stateful ?? false;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  getVersion(): number {
    return this.version;
  }

  private bump(): void {
    this.version += 1;
    for (const listener of [...this.listeners]) listener();
  }
}

/** The window's registry; tool panes register into it when their module loads. */
export const paneRegistry = new PaneRegistry();

const RegistryContext = createContext<PaneRegistry>(paneRegistry);

export function PaneRegistryProvider(props: { registry: PaneRegistry; children: ReactNode }) {
  return (
    <RegistryContext.Provider value={props.registry}>{props.children}</RegistryContext.Provider>
  );
}

export function usePaneRegistry(): PaneRegistry {
  const registry = useContext(RegistryContext);
  useSyncExternalStore(
    (listener) => registry.subscribe(listener),
    () => registry.getVersion(),
  );
  return registry;
}

export function useLayout(store: LayoutStore) {
  return useSyncExternalStore(
    (listener) => store.subscribe(listener),
    () => store.getSnapshot(),
  );
}

// -- titles --------------------------------------------------------------------------------------

const TITLE_KEYS: Readonly<Record<string, MessageKey>> = {
  chat: "panes.title.chat",
  preview: "panes.title.preview",
  files: "panes.title.files",
  review: "panes.title.review",
  terminal: "panes.title.terminal",
};

const PRESET_KEYS: Readonly<Record<PresetId, MessageKey>> = {
  basic: "panes.preset.basic",
  focus: "panes.preset.focus",
  default: "panes.preset.default",
  "terminal-deck": "panes.preset.terminalDeck",
  quad: "panes.preset.quad",
};

const MODE_KEYS: Readonly<Record<InterfaceMode, MessageKey>> = {
  advanced: "panes.mode.advanced",
  simple: "panes.mode.simple",
};

function usePaneTitle(): (pane: PaneId) => string {
  const t = useT();
  const registry = usePaneRegistry();
  return (pane) => {
    const key = TITLE_KEYS[pane];
    if (key !== undefined) return t(key);
    return registry.get(pane)?.title ?? pane;
  };
}

// -- drag and drop -------------------------------------------------------------------------------

export const PANE_DRAG_TYPE = "application/x-indus-pane";

/** Which part of a zone a pointer is over: the middle half is the centre, else the nearest edge. */
export function edgeAt(
  rect: { left: number; top: number; width: number; height: number },
  point: { x: number; y: number },
): DropEdge {
  if (rect.width <= 0 || rect.height <= 0) return "center";
  const x = (point.x - rect.left) / rect.width;
  const y = (point.y - rect.top) / rect.height;
  if (x > 0.25 && x < 0.75 && y > 0.25 && y < 0.75) return "center";
  const distances: Array<[DropEdge, number]> = [
    ["left", x],
    ["right", 1 - x],
    ["top", y],
    ["bottom", 1 - y],
  ];
  distances.sort((a, b) => a[1] - b[1]);
  return (distances[0] as [DropEdge, number])[0];
}

let draggedPane: PaneId | null = null;

function paneFromDrag(event: DragEvent): PaneId | null {
  const data = event.dataTransfer?.getData(PANE_DRAG_TYPE);
  return data !== undefined && data !== "" ? data : draggedPane;
}

// -- rendering -----------------------------------------------------------------------------------

/** What the chat tab's dot says about its conversation. */
export type ChatTabState = "idle" | "running" | "attention";

export interface PaneShellProps {
  store: LayoutStore;
  /** The chat pane's body (the route's content). */
  chat: ReactNode;
  sessionId: string | null;
  profile: string | null;
  /** Slot numbers on the tabs (the modifier is held). */
  showSlots?: boolean;
  /** Show the layout toolbar (presets, panes, interface mode). */
  toolbar?: boolean;
  /** Buttons the shell puts first in the title band's tool cluster (W51-b: Settings). */
  bandTools?: ReactNode;
  /** The chat tab's label (the conversation's title, or "New session"); else "Chat". */
  chatTitle?: string;
  /** The chat tab's dot (W54-D1-2): an approval waits, a turn runs, or quiet. */
  chatState?: ChatTabState;
  /** What tool panes get besides the route (P20-28..31). */
  workspace?: string | null;
  mode?: "local" | "remote" | null;
  connection?: ChatConnection | null;
  openPreview?: (target: PreviewTarget) => void;
  addToChat?: (text: string) => void;
}

interface ShellContext {
  store: LayoutStore;
  layout: PaneLayout;
  focusedZone: string | null;
  registry: PaneRegistry;
  chat: ReactNode;
  sessionId: string | null;
  profile: string | null;
  showSlots: boolean;
  title: (pane: PaneId) => string;
  chatState: ChatTabState | null;
  tools: Pick<PaneShellProps, "workspace" | "mode" | "connection" | "openPreview" | "addToChat">;
  /** The zone whose header runs under the band's tool cluster (top right), if any. */
  bandEnd: string | null;
  /** The cluster sits at the end of `bandEnd` rather than at the window's edge (W55-F1-3). */
  bandInner: boolean;
  /** The top-right zone when the cluster moved left of it: it keeps clear of the window controls. */
  bandEdge: string | null;
  /** Zones whose top edge is the window's top (their header is part of the title band). */
  topZones: ReadonlySet<string>;
  /** With the app actions on the left (W56-G1): the top-left zone, whose tabs start after them. */
  bandStart: string | null;
}

/** Panes that draw their own titled header (W52-m): alone in a zone they show no tab strip. */
const SELF_LABELLED: ReadonlySet<PaneId> = new Set(["files", "review"]);

/** The zones along the window's top edge: every child of a row, the first child of a column. */
export function topZoneIds(node: LayoutNode | null, into = new Set<string>()): Set<string> {
  if (node === null) return into;
  if (node.kind === "zone") into.add(node.id);
  else if (node.orientation === "row") for (const child of node.children) topZoneIds(child, into);
  else topZoneIds(node.children[0] ?? null, into);
  return into;
}

/** The top-right zone: the last child of a row, the first child of a column, down to a zone.
 * Collapsed children are skipped, since they take no room in the band. */
export function bandEndZone(layout: PaneLayout, node: LayoutNode | null): string | null {
  if (node === null) return null;
  if (node.kind === "zone") return node.id;
  const shown = node.children.filter((child) => !nodeCollapsed(layout, child));
  const pick = node.orientation === "row" ? shown[shown.length - 1] : shown[0];
  return pick === undefined ? null : bandEndZone(layout, pick);
}

/** The top-left zone: the first shown child of a row or a column, down to a zone (W56-G1). */
export function bandStartZone(layout: PaneLayout, node: LayoutNode | null): string | null {
  if (node === null) return null;
  if (node.kind === "zone") return node.id;
  const pick = node.children.find((child) => !nodeCollapsed(layout, child));
  return pick === undefined ? null : bandStartZone(layout, pick);
}

/** A top zone's box along the band, for placing the tool cluster. */
export interface BandZoneBox {
  readonly id: string;
  readonly left: number;
  readonly right: number;
  /** Its tabs are not drawn (a self-labelled pane): the cluster may cover them. */
  readonly selfLabelled: boolean;
}

export interface BandPlacement {
  /** The zone the cluster sits at the end of. */
  readonly zone: string;
  /** How far left of its usual place (the window controls' edge) the cluster sits, in px. */
  readonly shift: number;
  /** The top-right zone, when it is not `zone`. */
  readonly edge: string | null;
}

/** The room a zone's tabs keep beside the cluster before the cluster moves to a wider zone. */
export const BAND_TAB_ROOM = 140;
/** The gap the band keeps around the cluster (the CSS padding on the zone under it). */
const BAND_GAP = 16;

/**
 * Where the band's tool cluster goes (W55-F1-3): at the end of the rightmost top zone whose drawn
 * tabs keep `room` px beside it — a self-labelled zone draws none, so it always takes the
 * cluster — else at the window's edge as before. A narrow column with real tabs (Preview at the
 * window's right) used to lie wholly under the cluster: its tab could not be seen or clicked.
 * `controlsLeft` is the left edge of the window controls (the window's right edge without them).
 */
export function placeBandTools(
  boxes: readonly BandZoneBox[],
  controlsLeft: number,
  toolsWidth: number,
  room = BAND_TAB_ROOM,
): BandPlacement | null {
  const shown = boxes
    .filter((box) => box.right - box.left > 0)
    .sort((a, b) => a.left - b.left || a.right - b.right);
  const last = shown.at(-1);
  if (last === undefined) return null;
  for (let index = shown.length - 1; index >= 0; index--) {
    const box = shown[index] as BandZoneBox;
    const right = Math.min(box.right, controlsLeft);
    const free = right - box.left - toolsWidth - BAND_GAP;
    if (box.selfLabelled || free >= room) {
      const shift = Math.max(0, Math.round(controlsLeft - right));
      return { zone: box.id, shift, edge: box === last ? null : last.id };
    }
  }
  return { zone: last.id, shift: 0, edge: null };
}

function samePlacement(a: BandPlacement | null, b: BandPlacement | null): boolean {
  if (a === null || b === null) return a === b;
  return a.zone === b.zone && a.shift === b.shift && a.edge === b.edge;
}

/** A zone takes no space when it is minimised or every pane in it is hidden. */
function zoneCollapsed(layout: PaneLayout, z: ZoneNode): boolean {
  return z.minimized || z.tabs.every((pane) => isHidden(layout, pane));
}

function nodeCollapsed(layout: PaneLayout, node: LayoutNode): boolean {
  return node.kind === "zone"
    ? zoneCollapsed(layout, node)
    : node.children.every((child) => nodeCollapsed(layout, child));
}

function Separator(props: {
  ctx: ShellContext;
  node: SplitNode;
  index: number;
  container: () => HTMLElement | null;
}) {
  const t = useT();
  const { ctx, node, index } = props;
  const sizes = node.sizes;
  const total = sizes.reduce((sum, size) => sum + size, 0) || 1;
  const before = sizes.slice(0, index + 1).reduce((sum, size) => sum + size, 0);
  const nudge = (delta: number) => {
    const next = [...sizes];
    const a = (next[index] ?? 1) + delta * total;
    const b = (next[index + 1] ?? 1) - delta * total;
    if (a <= 0 || b <= 0) return;
    next[index] = a;
    next[index + 1] = b;
    ctx.store.resize(node.id, next);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const back = node.orientation === "row" ? "ArrowLeft" : "ArrowUp";
    const forward = node.orientation === "row" ? "ArrowRight" : "ArrowDown";
    if (event.key === back) nudge(-0.05);
    else if (event.key === forward) nudge(0.05);
    else return;
    event.preventDefault();
  };
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const host = props.container();
    if (host === null) return;
    const rect = host.getBoundingClientRect();
    const span = node.orientation === "row" ? rect.width : rect.height;
    if (span <= 0) return;
    const start = node.orientation === "row" ? event.clientX : event.clientY;
    const startSizes = [...sizes];
    const move = (moveEvent: globalThis.PointerEvent) => {
      const now = node.orientation === "row" ? moveEvent.clientX : moveEvent.clientY;
      const delta = ((now - start) / span) * total;
      const next = [...startSizes];
      const a = (next[index] ?? 1) + delta;
      const b = (next[index + 1] ?? 1) - delta;
      if (a <= total * 0.05 || b <= total * 0.05) return;
      next[index] = a;
      next[index + 1] = b;
      ctx.store.resize(node.id, next);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return (
    // biome-ignore lint/a11y/useSemanticElements: a focusable splitter (role separator), not an <hr>
    <div
      role="separator"
      tabIndex={0}
      aria-orientation={node.orientation === "row" ? "vertical" : "horizontal"}
      aria-valuenow={Math.round((before / total) * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={t("panes.resize")}
      className="pane-separator"
      data-orientation={node.orientation}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
    />
  );
}

function SplitView(props: { ctx: ShellContext; node: SplitNode }) {
  const { ctx, node } = props;
  const ref = useRef<HTMLDivElement>(null);
  const visible = node.children.map((child) => !nodeCollapsed(ctx.layout, child));
  const cells: ReactNode[] = [];
  node.children.forEach((child, index) => {
    const shown = visible[index] === true;
    const nextShown = visible.slice(index + 1).some(Boolean);
    cells.push(
      <div
        key={child.id}
        className="pane-cell"
        hidden={!shown}
        style={{ flexGrow: node.sizes[index] ?? 1 }}
      >
        <NodeView ctx={ctx} node={child} />
      </div>,
    );
    if (shown && nextShown) {
      cells.push(
        <Separator
          key={`${child.id}-sep`}
          ctx={ctx}
          node={node}
          index={index}
          container={() => ref.current}
        />,
      );
    }
  });
  return (
    <div ref={ref} className="pane-split" data-orientation={node.orientation}>
      {cells}
    </div>
  );
}

function NodeView(props: { ctx: ShellContext; node: LayoutNode }) {
  return props.node.kind === "zone" ? (
    <ZoneView ctx={props.ctx} zone={props.node} />
  ) : (
    <SplitView ctx={props.ctx} node={props.node} />
  );
}

function ZoneView(props: { ctx: ShellContext; zone: ZoneNode }) {
  const t = useT();
  const { ctx, zone: z } = props;
  const [drop, setDrop] = useState<DropEdge | null>(null);
  const collapsed = zoneCollapsed(ctx.layout, z);
  const active = z.active;
  const activeStateful = ctx.registry.isStateful(active);
  const canMinimize =
    !z.tabs.includes(CHAT_PANE) && !z.tabs.some((pane) => ctx.registry.isStateful(pane));
  const focused = ctx.focusedZone === z.id;
  const selfLabelled = z.tabs.length === 1 && SELF_LABELLED.has(z.tabs[0] as PaneId);
  const chrome = ctx.topZones.has(z.id) ? "band" : "strip";

  const onDragOver = (event: DragEvent<HTMLElement>) => {
    if (paneFromDrag(event) === null && !event.dataTransfer?.types.includes(PANE_DRAG_TYPE)) {
      return;
    }
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    setDrop(edgeAt(rect, { x: event.clientX, y: event.clientY }));
  };
  const onDrop = (event: DragEvent<HTMLElement>) => {
    const pane = paneFromDrag(event);
    setDrop(null);
    draggedPane = null;
    if (pane === null) return;
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    ctx.store.move(pane, {
      zoneId: z.id,
      edge: edgeAt(rect, { x: event.clientX, y: event.clientY }),
    });
  };

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number | null = null;
    if (event.key === "ArrowRight") next = (index + 1) % z.tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + z.tabs.length) % z.tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = z.tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    const pane = z.tabs[next] as PaneId;
    ctx.store.activate(pane);
    const host = event.currentTarget.parentElement;
    (host?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next] ?? null)?.focus();
  };

  return (
    <section
      className="pane-zone"
      data-zone-id={z.id}
      data-band-end={ctx.bandEnd === z.id ? (ctx.bandInner ? "inner" : "true") : undefined}
      data-band-edge={ctx.bandEdge === z.id && ctx.bandEnd !== z.id ? "true" : undefined}
      data-band-start={ctx.bandStart === z.id ? "true" : undefined}
      data-focused={focused ? "true" : "false"}
      data-chrome={chrome}
      data-self-labelled={selfLabelled ? "true" : undefined}
      data-tabs={z.tabs.length > 1 ? "many" : "one"}
      hidden={collapsed}
      aria-label={ctx.title(active)}
      onFocusCapture={() => ctx.store.setFocusedZone(z.id)}
      onPointerDownCapture={() => ctx.store.setFocusedZone(z.id)}
      onDragOver={onDragOver}
      onDragLeave={() => setDrop(null)}
      onDrop={onDrop}
    >
      <header className="pane-zone-header">
        <div className="pane-tabs" role="tablist" aria-label={t("panes.tabs")}>
          {z.tabs.map((pane, index) => {
            const selected = pane === active;
            const hidden = isHidden(ctx.layout, pane);
            return (
              <button
                key={pane}
                type="button"
                role="tab"
                id={`pane-tab-${pane}`}
                aria-selected={selected}
                aria-controls={`pane-body-${pane}`}
                tabIndex={selected ? 0 : -1}
                className="pane-tab"
                data-hidden={hidden ? "true" : undefined}
                draggable
                onDragStart={(event) => {
                  draggedPane = pane;
                  event.dataTransfer?.setData(PANE_DRAG_TYPE, pane);
                  if (event.dataTransfer !== undefined && event.dataTransfer !== null) {
                    event.dataTransfer.effectAllowed = "move";
                  }
                }}
                onDragEnd={() => {
                  draggedPane = null;
                }}
                onClick={() => ctx.store.activate(pane)}
                onKeyDown={(event) => onTabKeyDown(event, index)}
              >
                {ctx.showSlots && slotLabel(index, z.tabs.length) !== null && (
                  <span className="pane-slot" aria-hidden="true">
                    {slotLabel(index, z.tabs.length)}
                  </span>
                )}
                {pane === CHAT_PANE && ctx.chatState !== null && (
                  <i className="pane-tab-dot" data-state={ctx.chatState} aria-hidden="true" />
                )}
                <span>{ctx.title(pane)}</span>
                {pane === CHAT_PANE && ctx.chatState === "attention" && (
                  <span className="sr-only">{t("panes.chatWaiting")}</span>
                )}
                {hidden && <span className="sr-only">{t("panes.hiddenSuffix")}</span>}
              </button>
            );
          })}
        </div>
        <div className="pane-zone-actions">
          {activeStateful && !isHidden(ctx.layout, active) && (
            <button
              type="button"
              className="icon-button"
              aria-label={t("panes.hide", { pane: ctx.title(active) })}
              title={t("panes.hide", { pane: ctx.title(active) })}
              onClick={() => ctx.store.setHidden(active, true)}
            >
              <EyeOff aria-hidden="true" size={14} />
            </button>
          )}
          {canMinimize && (
            <button
              type="button"
              className="icon-button"
              aria-label={t("panes.minimize", { pane: ctx.title(active) })}
              title={t("panes.minimize", { pane: ctx.title(active) })}
              onClick={() => ctx.store.setMinimized(z.id, true)}
            >
              <Minus aria-hidden="true" size={14} />
            </button>
          )}
          {active !== CHAT_PANE && (
            <button
              type="button"
              className="icon-button"
              aria-label={t("panes.close", { pane: ctx.title(active) })}
              title={t("panes.close", { pane: ctx.title(active) })}
              onClick={() => ctx.store.close(active)}
            >
              <X aria-hidden="true" size={14} />
            </button>
          )}
        </div>
      </header>
      <div className="pane-bodies">
        {z.tabs.map((pane) => {
          const selected = pane === active;
          const hiddenByHide = isHidden(ctx.layout, pane);
          const keep = pane === CHAT_PANE || ctx.registry.isStateful(pane);
          const visible = selected && !hiddenByHide && !collapsed;
          if (!visible && !keep) return null;
          return (
            <div
              key={pane}
              id={`pane-body-${pane}`}
              role="tabpanel"
              aria-labelledby={`pane-tab-${pane}`}
              className="pane-body"
              data-pane={pane}
              hidden={!visible}
              inert={!visible}
            >
              <PaneBody ctx={ctx} pane={pane} active={visible} />
            </div>
          );
        })}
      </div>
      {drop !== null && <div className="pane-drop" data-edge={drop} aria-hidden="true" />}
    </section>
  );
}

/** The number a tab answers to (Mod+1..8; Mod+9 is always the last tab). */
export function slotLabel(index: number, count: number): number | null {
  if (index < 8) return index + 1;
  return index === count - 1 ? 9 : null;
}

function PaneBody(props: { ctx: ShellContext; pane: PaneId; active: boolean }) {
  const { ctx, pane } = props;
  if (pane === CHAT_PANE) return <>{ctx.chat}</>;
  const definition = ctx.registry.get(pane);
  if (definition === undefined) return null;
  return (
    <>
      {definition.render({
        sessionId: ctx.sessionId,
        profile: ctx.profile,
        active: props.active,
        hidden: !props.active,
        workspace: ctx.tools.workspace ?? null,
        mode: ctx.tools.mode ?? null,
        connection: ctx.tools.connection ?? null,
        ...(ctx.tools.openPreview === undefined ? {} : { openPreview: ctx.tools.openPreview }),
        ...(ctx.tools.addToChat === undefined ? {} : { addToChat: ctx.tools.addToChat }),
        close: () => ctx.store.close(pane),
      })}
    </>
  );
}

function RestoreRail(props: { ctx: ShellContext }) {
  const t = useT();
  const { ctx } = props;
  const minimized = zones(ctx.layout.root).filter((z) => z.minimized);
  const hidden = ctx.layout.hidden;
  if (minimized.length === 0 && hidden.length === 0) return null;
  return (
    <nav className="pane-rail" aria-label={t("panes.rail")}>
      {minimized.map((z) => (
        <button
          key={z.id}
          type="button"
          className="pane-rail-item"
          onClick={() => ctx.store.setMinimized(z.id, false)}
        >
          {t("panes.restore", { pane: ctx.title(z.active) })}
        </button>
      ))}
      {hidden.map((pane) => (
        <button
          key={pane}
          type="button"
          className="pane-rail-item"
          onClick={() => ctx.store.activate(pane)}
        >
          {t("panes.show", { pane: ctx.title(pane) })}
        </button>
      ))}
    </nav>
  );
}

/** Panes the toolbar flips with one button (W46): stateless, so closing one loses nothing. */
const TOGGLED_PANES: ReadonlySet<PaneId> = new Set(["files", "review"]);

/** Show a pane (open it, or bring it to the front of its zone) or, when it is showing, close it.
 * `data-ui` is the button's test hook; each use names it literally so the parity scenarios'
 * hook check finds it in the source. */
function PaneToggle(props: {
  ctx: ShellContext;
  mode: InterfaceMode;
  pane: PaneId;
  "data-ui": string;
}) {
  const { ctx, mode, pane } = props;
  if (!ctx.registry.ids().includes(pane) || !paneAllowed(mode, pane)) return null;
  const host = zones(ctx.layout.root).find((z) => z.tabs.includes(pane));
  const showing =
    host !== undefined && host.active === pane && !host.minimized && !isHidden(ctx.layout, pane);
  const title = ctx.title(pane);
  return (
    <button
      type="button"
      className="desk-icon-btn"
      data-size="band"
      aria-label={title}
      aria-pressed={showing}
      data-ui={props["data-ui"]}
      onClick={() => {
        if (showing) ctx.store.close(pane);
        else ctx.store.open(pane);
      }}
    >
      {paneIcon(pane)}
    </button>
  );
}

/** The title band's glyph for a tool pane (contributed panes share one). */
function paneIcon(pane: PaneId): ReactNode {
  switch (pane) {
    case "preview":
      return <Globe aria-hidden="true" />;
    case "files":
      return <FolderTree aria-hidden="true" />;
    case "review":
      return <GitCompareArrows aria-hidden="true" />;
    case "terminal":
      return <SquareTerminal aria-hidden="true" />;
    default:
      return <PanelRight aria-hidden="true" />;
  }
}

const MODE_ICONS: Readonly<Record<InterfaceMode, ReactNode>> = {
  advanced: <LayoutDashboard aria-hidden="true" />,
  simple: <RectangleHorizontal aria-hidden="true" />,
};

function LayoutToolbar(props: {
  ctx: ShellContext;
  mode: InterfaceMode;
  preset: PresetId | null;
  bandTools?: ReactNode;
  /** The layout editor (W52-l) is open. */
  editing?: boolean;
  onEdit?: () => void;
}) {
  const t = useT();
  const { ctx, mode } = props;
  const open = new Set(allPanes(ctx.layout.root));
  const closable = ctx.registry
    .ids()
    .filter(
      (pane) =>
        pane !== CHAT_PANE &&
        !TOGGLED_PANES.has(pane) &&
        !open.has(pane) &&
        paneAllowed(mode, pane),
    );
  return (
    <div className="pane-toolbar">
      {props.bandTools}
      <label className="pane-toolbar-field">
        <span className="sr-only">{t("panes.layout")}</span>
        <LayoutTemplate aria-hidden="true" className="pane-toolbar-glyph" />
        <select
          value={props.preset ?? ""}
          onChange={(event) => {
            const value = event.currentTarget.value;
            if ((PRESET_IDS as readonly string[]).includes(value)) {
              ctx.store.applyPreset(value as PresetId);
            }
          }}
        >
          <option value="">{t("panes.preset.custom")}</option>
          {PRESET_IDS.map((id) => (
            <option key={id} value={id}>
              {t(PRESET_KEYS[id])}
            </option>
          ))}
        </select>
      </label>
      {props.onEdit !== undefined && (
        <button
          type="button"
          className="desk-icon-btn"
          data-size="band"
          aria-label={t("lay.open")}
          aria-pressed={props.editing === true}
          data-ui="layout-editor"
          onClick={props.onEdit}
        >
          <LayoutGrid aria-hidden="true" />
        </button>
      )}
      <PaneToggle ctx={ctx} mode={mode} pane="files" data-ui="pane-toggle-files" />
      <PaneToggle ctx={ctx} mode={mode} pane="review" data-ui="pane-toggle-review" />
      {closable.map((pane) => (
        <button
          key={pane}
          type="button"
          className="desk-icon-btn"
          data-size="band"
          aria-label={t("panes.open", { pane: ctx.title(pane) })}
          onClick={() => ctx.store.open(pane)}
        >
          {paneIcon(pane)}
        </button>
      ))}
      <div className="band-modes" role="radiogroup" aria-label={t("panes.mode.label")}>
        {INTERFACE_MODES.map((option) => (
          // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={mode === option}
            aria-label={t(MODE_KEYS[option])}
            className="desk-icon-btn"
            data-size="band"
            onClick={() => ctx.store.setMode(option)}
          >
            {MODE_ICONS[option]}
          </button>
        ))}
      </div>
    </div>
  );
}

export function PaneShell(props: PaneShellProps) {
  const registry = usePaneRegistry();
  // the layout editor (W52-l): the band button or Ctrl+Shift+\ toggles it
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (props.toolbar === false) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.code === "Backslash") {
        event.preventDefault();
        setEditing((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.toolbar]);
  const snapshot = useLayout(props.store);
  const paneTitle = usePaneTitle();
  const chatTitle = props.chatTitle;
  const title = (pane: PaneId) =>
    pane === CHAT_PANE && chatTitle !== undefined && chatTitle !== "" ? chatTitle : paneTitle(pane);
  const knownKey = registry.ids().join("\n");
  const layout = useMemo(
    () => withoutUnknown(snapshot.layout, new Set(knownKey.split("\n"))),
    [snapshot.layout, knownKey],
  );
  const ctx: ShellContext = {
    store: props.store,
    layout,
    focusedZone: snapshot.focusedZone,
    registry,
    chat: props.chat,
    sessionId: props.sessionId,
    profile: props.profile,
    showSlots: props.showSlots === true,
    title,
    chatState: props.chatState ?? null,
    tools: {
      workspace: props.workspace ?? null,
      mode: props.mode ?? null,
      connection: props.connection ?? null,
      ...(props.openPreview === undefined ? {} : { openPreview: props.openPreview }),
      ...(props.addToChat === undefined ? {} : { addToChat: props.addToChat }),
    },
    bandEnd: null,
    bandInner: false,
    bandEdge: null,
    topZones: topZoneIds(layout.root),
    bandStart: null,
  };
  const [band, setBand] = useState<BandPlacement | null>(null);
  // App actions (W56-G1): the band's tool cluster at the pane area's left end instead of at the
  // window controls; the top-left zone's tabs start after it, the top-right one keeps clear of
  // the window controls only, and the W55-F1-3 shift does not apply
  const display = useDisplayPrefs();
  const actionsLeft = props.toolbar !== false && display.appActionsSide === "left";
  const actionsLeftRef = useRef(actionsLeft);
  actionsLeftRef.current = actionsLeft;
  if (actionsLeft) {
    ctx.bandStart = bandStartZone(layout, layout.root);
    ctx.bandEdge = bandEndZone(layout, layout.root);
  } else if (props.toolbar !== false) {
    // the measured placement while its zones are still on the band; the tree's guess before
    const measured =
      band !== null &&
      ctx.topZones.has(band.zone) &&
      (band.edge === null || ctx.topZones.has(band.edge))
        ? band
        : null;
    ctx.bandEnd = measured?.zone ?? bandEndZone(layout, layout.root);
    ctx.bandInner = (measured?.shift ?? 0) > 0;
    ctx.bandEdge = measured?.edge ?? null;
  }
  const root = layout.root;
  const shellRef = useRef<HTMLDivElement>(null);
  // place the band's tool cluster from the zones' real boxes (W55-F1-3), after every layout
  // change and whenever the shell or the cluster resizes
  const placeBand = useCallback(() => {
    const host = shellRef.current;
    const tools = host?.querySelector<HTMLElement>(":scope > .pane-toolbar") ?? null;
    if (host === null || tools === null) return;
    if (actionsLeftRef.current) {
      host.style.removeProperty("--band-shift");
      setBand(null);
      return;
    }
    // the cluster's box less the shift applied now: its usual place, at the window controls
    const applied = Number.parseFloat(host.style.getPropertyValue("--band-shift")) || 0;
    const usual = tools.getBoundingClientRect();
    const boxes: BandZoneBox[] = [
      ...host.querySelectorAll<HTMLElement>(
        ":scope > .pane-root .pane-zone[data-chrome='band']:not([hidden])",
      ),
    ].map((zone) => {
      const box = zone.getBoundingClientRect();
      return {
        id: zone.dataset.zoneId ?? "",
        left: box.left,
        right: box.right,
        selfLabelled: zone.dataset.selfLabelled === "true",
      };
    });
    const controlsLeft = usual.right + applied + 8;
    const next = usual.width > 0 ? placeBandTools(boxes, controlsLeft, tools.offsetWidth) : null;
    const shift = next?.shift ?? 0;
    if (shift !== applied) {
      if (shift > 0) host.style.setProperty("--band-shift", `${shift}px`);
      else host.style.removeProperty("--band-shift");
    }
    setBand((before) => (samePlacement(before, next) ? before : next));
  }, []);
  // biome-ignore lint/correctness/useExhaustiveDependencies: placed again when the side changes
  useLayoutEffect(() => {
    if (props.toolbar === false || layout.root === null || typeof ResizeObserver === "undefined")
      return;
    placeBand();
  }, [layout, placeBand, props.toolbar, actionsLeft]);
  // File browser (W56-G1): each workspace folder the window gets shows the Files pane once;
  // closing it lasts until the folder changes (or the setting is turned off and on again)
  const workspace = props.workspace ?? null;
  const hasFiles = registry.ids().includes("files");
  const shownFor = useRef<string | null>(null);
  const store = props.store;
  useEffect(() => {
    if (props.toolbar === false) return;
    if (!display.fileBrowser || workspace === null) {
      shownFor.current = null;
      return;
    }
    if (!hasFiles || shownFor.current === workspace) return;
    shownFor.current = workspace;
    const current = store.layout();
    const host = zones(current.root).find((z) => z.tabs.includes("files"));
    const showing =
      host !== undefined &&
      host.active === "files" &&
      !host.minimized &&
      !isHidden(current, "files");
    if (!showing) store.open("files");
  }, [display.fileBrowser, workspace, hasFiles, store, props.toolbar]);
  useEffect(() => {
    const host = shellRef.current;
    const tools = host?.querySelector<HTMLElement>(":scope > .pane-toolbar") ?? null;
    if (host === null || tools === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => placeBand());
    observer.observe(host);
    observer.observe(tools);
    return () => observer.disconnect();
  }, [placeBand]);
  // the band's tool cluster width, so the zone under it keeps its tabs clear (W51-b)
  useEffect(() => {
    const host = shellRef.current;
    const tools = host?.querySelector<HTMLElement>(":scope > .pane-toolbar") ?? null;
    if (host === null || tools === null || typeof ResizeObserver === "undefined") return;
    const sync = () => host.style.setProperty("--band-tools-width", `${tools.offsetWidth}px`);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(tools);
    return () => observer.disconnect();
  }, []);
  return (
    <div
      className="pane-shell"
      data-mode={snapshot.mode}
      data-app-actions={actionsLeft ? "left" : undefined}
      data-layout-editing={editing ? "true" : undefined}
      ref={shellRef}
    >
      {props.toolbar !== false && (
        <LayoutToolbar
          ctx={ctx}
          mode={snapshot.mode}
          preset={snapshot.preset}
          bandTools={props.bandTools}
          editing={editing}
          onEdit={() => setEditing(!editing)}
        />
      )}
      <RestoreRail ctx={ctx} />
      <div className="pane-root">{root !== null && <NodeView ctx={ctx} node={root} />}</div>
      {editing && (
        <LayoutEditor
          store={props.store}
          mode={snapshot.mode}
          preset={snapshot.preset}
          panes={registry.ids()}
          onDone={() => setEditing(false)}
        />
      )}
    </div>
  );
}

/** For shortcuts: the active pane of the focused zone. */
export function focusedPane(store: LayoutStore): PaneId | null {
  const snapshot = store.getSnapshot();
  const z = zones(snapshot.layout.root).find((candidate) => candidate.id === snapshot.focusedZone);
  return z?.active ?? null;
}
