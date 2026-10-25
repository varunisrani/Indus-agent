// The layout editor card (W52-l, spec 3.14): a floating "Layouts" card opened from the band (or
// Ctrl+Shift+\) while the pane shell dashes every zone. It sets the interface mode (Simple /
// Advanced cards) and applies a template, each drawn as a small miniature of its preset tree.
// Our store has no saved custom templates, so the "Custom" actions are disabled placeholders
// (W52-l-7). Dragging panes between zones keeps using the zones' own drag and drop.

import { Grid2x2Plus, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { Button, Kbd } from "../ui/components.tsx";
import type { LayoutStore } from "./layoutStore.ts";
import type { LayoutNode, PaneId } from "./paneTree.ts";
import {
  INTERFACE_MODES,
  type InterfaceMode,
  PRESET_IDS,
  type PresetId,
  presetTree,
} from "./presets.ts";
import "./layoutEditor.css";

const OPTIONAL = new Set<PaneId>(["files", "review", "terminal", "preview"]);

/** A miniature of a layout tree: flex boxes, optional panes dashed. */
export function Miniature(props: { node: LayoutNode }) {
  const { node } = props;
  if (node.kind === "zone") {
    return (
      <span
        className="lay-mini-zone"
        data-optional={node.tabs.every((pane) => OPTIONAL.has(pane)) ? "true" : undefined}
      />
    );
  }
  return (
    <span className="lay-mini-split" data-orientation={node.orientation}>
      {node.children.map((child, index) => (
        <span key={child.id} className="lay-mini-cell" style={{ flexGrow: node.sizes[index] ?? 1 }}>
          <Miniature node={child} />
        </span>
      ))}
    </span>
  );
}

const PRESET_KEYS: Readonly<Record<PresetId, MessageKey>> = {
  basic: "panes.preset.basic",
  focus: "panes.preset.focus",
  default: "panes.preset.default",
  "terminal-deck": "panes.preset.terminalDeck",
  quad: "panes.preset.quad",
};

/** The reference's order: Default first. */
const TEMPLATE_ORDER: readonly PresetId[] = ["default", "basic", "focus", "terminal-deck", "quad"];

export function LayoutEditor(props: {
  store: LayoutStore;
  mode: InterfaceMode;
  preset: PresetId | null;
  panes: readonly PaneId[];
  onDone: () => void;
}) {
  const t = useT();
  const card = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; from: { x: number; y: number } } | null>(null);
  useEffect(() => {
    card.current?.focus();
  }, []);
  const order = TEMPLATE_ORDER.filter((id) => (PRESET_IDS as readonly string[]).includes(id));
  return (
    <div
      ref={card}
      className="lay-card"
      role="dialog"
      aria-label={t("lay.title")}
      tabIndex={-1}
      style={{ transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))` }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          props.onDone();
        }
      }}
    >
      <header
        className="lay-head"
        onPointerDown={(event) => {
          if ((event.target as HTMLElement).closest("button") !== null) return;
          drag.current = { x: event.clientX, y: event.clientY, from: offset };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          const start = drag.current;
          if (start === null) return;
          setOffset({
            x: start.from.x + event.clientX - start.x,
            y: start.from.y + event.clientY - start.y,
          });
        }}
        onPointerUp={() => {
          drag.current = null;
        }}
      >
        <div className="lay-head-text">
          <h2>{t("lay.title")}</h2>
          <p>
            {t("lay.hint")} <Kbd>Ctrl+Shift+\</Kbd>
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={() => props.store.reset()}>
          {t("lay.reset")}
        </Button>
        <Button variant="outline" size="sm" onClick={props.onDone}>
          {t("lay.done")}
        </Button>
      </header>
      <div className="lay-body">
        <section>
          <h3 className="lay-label">{t("lay.mode")}</h3>
          <div className="lay-modes" role="radiogroup" aria-label={t("panes.mode.label")}>
            {INTERFACE_MODES.map((mode) => (
              // biome-ignore lint/a11y/useSemanticElements: a card radio, not a native input
              <button
                key={mode}
                type="button"
                role="radio"
                aria-checked={props.mode === mode}
                className="lay-option"
                onClick={() => props.store.setMode(mode)}
              >
                <span className="lay-option-title">{t(`panes.mode.${mode}`)}</span>
                <span className="lay-option-text">{t(`lay.mode.${mode}`)}</span>
              </button>
            ))}
          </div>
          <p className="lay-note">{t("lay.modeNote")}</p>
        </section>
        <section>
          <h3 className="lay-label">{t("lay.templates")}</h3>
          <div className="lay-templates" role="radiogroup" aria-label={t("lay.templates")}>
            {order.map((id) => (
              // biome-ignore lint/a11y/useSemanticElements: a card radio, not a native input
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={props.preset === id}
                className="lay-template"
                disabled={props.mode === "simple"}
                onClick={() => props.store.applyPreset(id)}
              >
                <span className="lay-mini" aria-hidden="true">
                  <Miniature node={presetTree(id, props.panes)} />
                </span>
                <span className="lay-template-name">{t(PRESET_KEYS[id])}</span>
              </button>
            ))}
          </div>
        </section>
        <section>
          <h3 className="lay-label">{t("lay.custom")}</h3>
          <button type="button" className="lay-new" disabled title={t("cc.notYet")}>
            <Grid2x2Plus aria-hidden="true" size={13} />
            {t("lay.newGrid")}
          </button>
          <button type="button" className="lay-save" disabled title={t("cc.notYet")}>
            <Save aria-hidden="true" size={12} />
            {t("lay.saveCurrent")}
          </button>
        </section>
      </div>
    </div>
  );
}
