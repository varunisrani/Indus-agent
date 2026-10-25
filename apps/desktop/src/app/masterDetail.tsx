// The list-and-detail frame of the Capabilities and Messaging pages (W52-k): a one-row page
// header (search | centred text tabs | trailing slot), two columns split by a draggable seam whose
// width is remembered per page, switch rows, a centred detail column with a pinned footer, and a
// resizable bottom section (the Skills Hub). Styled by `masterDetail.css` on the `--desk-*` tokens.
// The small controls inside (text tabs, search, pills, meta rows, detail title, text actions) are
// the shared ones in `kit.tsx`; empty and error panels are the foundation's `EmptyState`.

import { ArrowDown, ArrowUp, EllipsisVertical } from "lucide-react";
import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Switch } from "../settings/primitives.tsx";
import { IconButton, MenuPanel } from "../ui/components.tsx";
import { TextAction } from "./kit.tsx";
import "./masterDetail.css";

// -- remembered sizes ------------------------------------------------------------------------------

function readNumber(key: string): number | null {
  try {
    const raw = window.localStorage.getItem(key);
    const value = raw === null ? Number.NaN : Number(raw);
    return Number.isFinite(value) ? value : null;
  } catch {
    return null;
  }
}

function writeValue(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // storage blocked: the size just is not remembered
  }
}

/** Keep `value` inside `[min, max]` (min wins when the range is empty). */
export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, max));
}

// -- page header -----------------------------------------------------------------------------------

/** The page's single header row: search at the left, tabs in the centre, a trailing slot. */
export function PageHeader(props: { search?: ReactNode; tabs?: ReactNode; trailing?: ReactNode }) {
  return (
    <header className="md-header">
      <div className="md-header-start">{props.search}</div>
      <div className="md-header-centre">{props.tabs}</div>
      <div className="md-header-end">{props.trailing}</div>
    </header>
  );
}

/** "Try "term"" — the placeholder walks through `terms` every few seconds. */
export function usePlaceholderTerm(terms: readonly string[], everyMs = 3_000): string | null {
  const [at, setAt] = useState(0);
  useEffect(() => {
    if (terms.length < 2) return;
    const timer = setInterval(() => setAt((n) => n + 1), everyMs);
    return () => clearInterval(timer);
  }, [terms.length, everyMs]);
  return terms.length === 0 ? null : (terms[at % terms.length] ?? null);
}

// -- two columns -----------------------------------------------------------------------------------

const MIN_LIST = 180;
const MIN_DETAIL = 320;

/**
 * List and detail side by side. `wide`: the list takes 0.75fr until the seam is dragged (the
 * width is kept per `id`; a double-click forgets it). `rail`: a fixed 224px list.
 */
export function MasterDetail(props: {
  id: string;
  variant?: "wide" | "rail";
  list: ReactNode;
  detail: ReactNode;
  listLabel?: string;
}) {
  const variant = props.variant ?? "wide";
  const key = `indus.masterDetail.${props.id}`;
  const [width, setWidth] = useState<number | null>(() =>
    variant === "wide" ? readNumber(key) : null,
  );
  const root = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);

  const onDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const box = root.current?.getBoundingClientRect();
    if (box === undefined) return;
    event.preventDefault();
    setDragging(true);
    const move = (moved: PointerEvent) => {
      const next = clamp(moved.clientX - box.left, MIN_LIST, box.width - MIN_DETAIL);
      setWidth(Math.round(next));
    };
    const up = () => {
      setDragging(false);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setWidth((current) => {
        if (current !== null) writeValue(key, String(current));
        return current;
      });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const columns =
    variant === "rail"
      ? "224px minmax(0, 1fr)"
      : width === null
        ? "minmax(0, 0.75fr) minmax(0, 1fr)"
        : `${width}px minmax(0, 1fr)`;
  return (
    <div
      ref={root}
      className="md-frame"
      data-variant={variant}
      data-dragging={dragging ? "true" : undefined}
      style={{ gridTemplateColumns: columns }}
    >
      <nav className="md-list" aria-label={props.listLabel}>
        {props.list}
      </nav>
      <div className="md-detail-wrap">
        {variant === "wide" && (
          <div
            className="md-sash"
            aria-hidden="true"
            onPointerDown={onDown}
            onDoubleClick={() => {
              setWidth(null);
              writeValue(key, null);
            }}
          />
        )}
        {props.detail}
      </div>
    </div>
  );
}

/** The strip over a list: a sort toggle at the left, a kebab menu at the right. */
export function ListStrip(props: {
  sortLabel: string;
  descending: boolean;
  onSort: () => void;
  menuLabel?: string;
  menu?: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLSpanElement>(null);
  const close = useCallback(() => setOpen(false), []);
  return (
    <div className="md-strip">
      <button type="button" className="md-sort" onClick={props.onSort}>
        {props.descending ? (
          <ArrowDown aria-hidden="true" size={11} />
        ) : (
          <ArrowUp aria-hidden="true" size={11} />
        )}
        {props.sortLabel}
      </button>
      {props.menu !== undefined && (
        <span className="md-strip-menu" ref={trigger}>
          <IconButton
            label={props.menuLabel ?? ""}
            icon={<EllipsisVertical aria-hidden="true" size={14} />}
            size="sm"
            aria-haspopup="menu"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          />
          {open && (
            <MenuPanel
              label={props.menuLabel ?? ""}
              onClose={close}
              keepOpenFor={trigger}
              className="md-strip-panel"
            >
              {props.menu(close)}
            </MenuPanel>
          )}
        </span>
      )}
    </div>
  );
}

/** The small switch of the rows (28x16, dimmed while off). */
export function RowSwitch(props: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  title?: string;
  onChange: (on: boolean) => void;
}) {
  return (
    <span className="md-switch" title={props.title}>
      <Switch
        size="sm"
        label={props.label}
        checked={props.checked}
        {...(props.disabled === undefined ? {} : { disabled: props.disabled })}
        onChange={props.onChange}
      />
    </span>
  );
}

/** One selectable row: title, optional subtitle and badge, a meta chip and a trailing control. */
export function CapRow(props: {
  title: string;
  subtitle?: string;
  badge?: ReactNode;
  meta?: string;
  selected: boolean;
  dim?: boolean;
  control?: ReactNode;
  onSelect: () => void;
  testId?: string;
}) {
  const hasSub = props.subtitle !== undefined && props.subtitle !== "";
  return (
    <li
      className="md-row"
      data-selected={props.selected ? "true" : undefined}
      data-dim={props.dim ? "true" : undefined}
      data-tall={hasSub ? "true" : undefined}
      data-testid={props.testId}
    >
      <button
        type="button"
        className="md-row-main"
        aria-current={props.selected ? "true" : undefined}
        onClick={props.onSelect}
      >
        <span className="md-row-title">{props.title}</span>
        {(hasSub || props.badge !== undefined) && (
          <span className="md-row-sub">
            {hasSub && <span className="md-row-sub-text">{props.subtitle}</span>}
            {props.badge}
          </span>
        )}
      </button>
      {props.meta !== undefined && <span className="md-meta">{props.meta}</span>}
      {props.control !== undefined && <span className="md-row-control">{props.control}</span>}
    </li>
  );
}

/** The detail column: centred content that scrolls, and a pinned footer (caption or action bar). */
export function DetailColumn(props: { children: ReactNode; footer?: ReactNode; label?: string }) {
  return (
    <section className="md-detail" aria-label={props.label}>
      <div className="md-detail-scroll">
        <div className="md-detail-body">{props.children}</div>
      </div>
      {props.footer !== undefined && <div className="md-detail-footer">{props.footer}</div>}
    </section>
  );
}

/** A monospace chip (a tool name). */
export function ToolChip(props: { children: ReactNode }) {
  return <li className="md-tool-chip">{props.children}</li>;
}

/** The uppercase tracked title of a detail section. */
export function DetailSection(props: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="md-section">
      <h3 className="md-section-title">
        <span>{props.title}</span>
        {props.aside}
      </h3>
      {props.children}
    </section>
  );
}

// -- bottom section --------------------------------------------------------------------------------

const MIN_BOTTOM = 120;
const KEEP_ABOVE = 176;

/**
 * A section docked under the list and detail (the Skills Hub, the plugin catalog): a primary-tint
 * line on top that drags to resize, a 32px header (label, actions, show/hide), a framed body and
 * a hint line. Height and the collapsed state are kept per `id`.
 */
export function BottomSection(props: {
  id: string;
  label: string;
  upper?: boolean;
  actions?: ReactNode;
  hideLabel: string;
  showLabel: string;
  hint?: string;
  defaultCollapsed?: boolean;
  defaultHeight?: number;
  children: ReactNode;
}) {
  const heightKey = `indus.bottomSection.${props.id}.height`;
  const collapsedKey = `indus.bottomSection.${props.id}.collapsed`;
  const [height, setHeight] = useState(() => readNumber(heightKey) ?? props.defaultHeight ?? 380);
  const [collapsed, setCollapsed] = useState(() => {
    const stored = readNumber(collapsedKey);
    return stored === null ? props.defaultCollapsed === true : stored === 1;
  });
  const root = useRef<HTMLElement>(null);

  const onDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (collapsed) return;
    const parent = root.current?.parentElement?.getBoundingClientRect();
    if (parent === undefined) return;
    event.preventDefault();
    const startY = event.clientY;
    const start = height;
    const max = Math.min(window.innerHeight * 0.75, parent.height - KEEP_ABOVE);
    let last = start;
    const move = (moved: PointerEvent) => {
      last = Math.round(clamp(start - (moved.clientY - startY), MIN_BOTTOM, max));
      setHeight(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      writeValue(heightKey, String(last));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    writeValue(collapsedKey, next ? "1" : "0");
  };

  return (
    <section
      ref={root}
      className="md-bottom"
      aria-label={props.label}
      data-collapsed={collapsed ? "true" : undefined}
    >
      <div className="md-bottom-edge" aria-hidden="true" onPointerDown={onDown} />
      <div className="md-bottom-head">
        <span className="md-bottom-label" data-upper={props.upper ? "true" : undefined}>
          {props.label}
        </span>
        <span className="md-bottom-actions">
          {props.actions}
          <TextAction onClick={toggle}>{collapsed ? props.showLabel : props.hideLabel}</TextAction>
        </span>
      </div>
      {!collapsed && (
        <div className="md-bottom-body" style={{ height }}>
          <div className="md-bottom-frame">{props.children}</div>
          {props.hint !== undefined && <p className="md-bottom-hint">{props.hint}</p>}
        </div>
      )}
    </section>
  );
}
