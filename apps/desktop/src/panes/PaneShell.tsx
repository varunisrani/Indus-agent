// The pane shell (P20-27): renders the layout tree of `layoutStore.ts`. Zones show their panes as
// tabs; tabs drag onto another zone's centre (join) or edge (split). A stateful pane (preview,
// terminal) is never unmounted while it is open: Hide keeps its body in the DOM, `hidden` and
// `inert`; Close releases it. Zones without stateful panes (and without the chat) minimise to the
// restore rail. Unfocused zones are desaturated by the stylesheet (`data-focused`).
//
// Tool panes register here (P20-28..31): `paneRegistry.register({ id, stateful, render })`.

import { EyeOff, Minus, X } from "lucide-react";
import {
  createContext,
  type DragEvent,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  useContext,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { ChatConnection } from "../gateway/connectionStore.ts";
import { type MessageKey, useT } from "../i18n/index.tsx";
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
  tools: Pick<PaneShellProps, "workspace" | "mode" | "connection" | "openPreview" | "addToChat">;
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
      data-focused={focused ? "true" : "false"}
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
                <span>{ctx.title(pane)}</span>
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

function LayoutToolbar(props: { ctx: ShellContext; mode: InterfaceMode; preset: PresetId | null }) {
  const t = useT();
  const { ctx, mode } = props;
  const open = new Set(allPanes(ctx.layout.root));
  const closable = ctx.registry
    .ids()
    .filter((pane) => pane !== CHAT_PANE && !open.has(pane) && paneAllowed(mode, pane));
  return (
    <div className="pane-toolbar">
      <label className="pane-toolbar-field">
        <span>{t("panes.layout")}</span>
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
      {closable.map((pane) => (
        <button
          key={pane}
          type="button"
          className="text-button"
          onClick={() => ctx.store.open(pane)}
        >
          {t("panes.open", { pane: ctx.title(pane) })}
        </button>
      ))}
      <div className="segmented" role="radiogroup" aria-label={t("panes.mode.label")}>
        {INTERFACE_MODES.map((option) => (
          // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={mode === option}
            className="segmented-option segmented-text"
            onClick={() => ctx.store.setMode(option)}
          >
            {t(MODE_KEYS[option])}
          </button>
        ))}
      </div>
    </div>
  );
}

export function PaneShell(props: PaneShellProps) {
  const registry = usePaneRegistry();
  const snapshot = useLayout(props.store);
  const title = usePaneTitle();
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
    tools: {
      workspace: props.workspace ?? null,
      mode: props.mode ?? null,
      connection: props.connection ?? null,
      ...(props.openPreview === undefined ? {} : { openPreview: props.openPreview }),
      ...(props.addToChat === undefined ? {} : { addToChat: props.addToChat }),
    },
  };
  const root = layout.root;
  return (
    <div className="pane-shell" data-mode={snapshot.mode}>
      {props.toolbar !== false && (
        <LayoutToolbar ctx={ctx} mode={snapshot.mode} preset={snapshot.preset} />
      )}
      <RestoreRail ctx={ctx} />
      <div className="pane-root">{root !== null && <NodeView ctx={ctx} node={root} />}</div>
    </div>
  );
}

/** For shortcuts: the active pane of the focused zone. */
export function focusedPane(store: LayoutStore): PaneId | null {
  const snapshot = store.getSnapshot();
  const z = zones(snapshot.layout.root).find((candidate) => candidate.id === snapshot.focusedZone);
  return z?.active ?? null;
}
