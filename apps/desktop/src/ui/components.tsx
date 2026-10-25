// Core desktop components (W51-b). Small, styled by `ui.css` on the `--desk-*` tokens; pages pick
// a component and a variant instead of restyling at the call site. See
// docs/notes/ui-parity/desktop-foundation.md for the catalogue.

import * as Dialog from "@radix-ui/react-dialog";
import { Check, ChevronDown, ChevronRight, X } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import "./ui.css";

export type ButtonVariant = "primary" | "secondary" | "outline" | "ghost" | "text" | "danger";
export type ButtonSize = "sm" | "md";

/** The one text button: pick a variant and a size, never restyle padding or colour. */
export function Button(
  props: ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: ButtonVariant;
    size?: ButtonSize;
    icon?: ReactNode;
  },
) {
  const { variant = "secondary", size = "md", icon, className, children, ...rest } = props;
  return (
    <button
      type="button"
      {...rest}
      className={className === undefined ? "desk-btn" : `desk-btn ${className}`}
      data-variant={variant}
      data-size={size}
    >
      {icon}
      {children}
    </button>
  );
}

/** A square icon-only button; `label` is its accessible name (no native tooltip). */
export function IconButton(
  props: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
    label: string;
    icon: ReactNode;
    size?: "sm" | "md" | "band";
    active?: boolean;
  },
) {
  const { label, icon, size = "md", active, className, ...rest } = props;
  return (
    <button
      type="button"
      aria-label={label}
      {...rest}
      className={className === undefined ? "desk-icon-btn" : `desk-icon-btn ${className}`}
      data-size={size}
      data-active={active ? "true" : undefined}
    >
      {icon}
    </button>
  );
}

/** One key cap. */
export function Kbd(props: { children: ReactNode }) {
  return <kbd className="desk-kbd">{props.children}</kbd>;
}

/**
 * A shortcut as key caps (`["Ctrl", "N"]`). The caps are drawn from a data attribute, so they add
 * nothing to the text or the accessible name of the button they sit in (the action names itself).
 */
export function KeyHint(props: { keys: readonly string[] }) {
  return (
    <span className="desk-key-hint" aria-hidden="true">
      {props.keys.map((key) => (
        <kbd key={key} className="desk-kbd" data-key={key} />
      ))}
    </span>
  );
}

/** The small uppercase accent label over a sidebar or settings group. */
export function SectionLabel(props: {
  icon?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="desk-section-label">
      {props.icon}
      <span className="desk-section-label-text">{props.children}</span>
      {props.actions !== undefined && (
        <span className="desk-section-label-actions">{props.actions}</span>
      )}
    </div>
  );
}

/** The four-square glyph the sidebar's group labels carry (drawn, not an icon file). */
export function GroupGlyph() {
  return (
    <svg className="desk-group-glyph" viewBox="0 0 10 10" aria-hidden="true">
      <rect x="0.5" y="0.5" width="4" height="4" rx="0.6" />
      <rect x="5.5" y="0.5" width="4" height="4" rx="0.6" />
      <rect x="0.5" y="5.5" width="4" height="4" rx="0.6" />
      <rect x="5.5" y="5.5" width="4" height="4" rx="0.6" />
    </svg>
  );
}

/** A quiet picker trigger (composer model / effort): label and a small chevron. */
export function Chip(
  props: ButtonHTMLAttributes<HTMLButtonElement> & { children: ReactNode; caret?: boolean },
) {
  const { caret = true, className, children, ...rest } = props;
  return (
    <button
      type="button"
      {...rest}
      className={className === undefined ? "desk-chip" : `desk-chip ${className}`}
    >
      <span className="desk-chip-label">{children}</span>
      {caret && <ChevronDown aria-hidden="true" size={12} className="desk-chip-caret" />}
    </button>
  );
}

/** The large serif product mark of the new-chat screen. */
export function Wordmark(props: { text: string; caption?: ReactNode }) {
  return (
    <div className="desk-wordmark-block">
      <h1 className="desk-wordmark">{props.text}</h1>
      {props.caption !== undefined && <p className="desk-wordmark-caption">{props.caption}</p>}
    </div>
  );
}

/** A centred empty state for a list or panel: optional icon, one line, optional action. */
export function EmptyState(props: {
  icon?: ReactNode;
  title: ReactNode;
  action?: ReactNode;
  /** A quieter line under the title (W52-l panel empty states). */
  description?: ReactNode;
  /** `panel`: the overlay panels' look (20px icon, 14px title, 12px text, max 384px). */
  look?: "panel";
  /** Grow and centre in a column (the whole body of a page or tab has nothing to show). */
  fill?: boolean;
  /** `alert` for a load failure, `status` for a live "nothing here". */
  role?: "alert" | "status";
}) {
  return (
    <div
      className="desk-empty"
      data-look={props.look}
      data-fill={props.fill ? "true" : undefined}
      role={props.role}
    >
      {props.icon !== undefined && <span className="desk-empty-icon">{props.icon}</span>}
      <p className="desk-empty-title">{props.title}</p>
      {props.description !== undefined && <p className="desk-empty-text">{props.description}</p>}
      {props.action}
    </div>
  );
}

export interface MenuItem {
  readonly id: string;
  readonly label: string;
  readonly icon?: ReactNode;
  readonly hint?: string;
  /** `danger` paints the item in the danger colour (e.g. a reject choice). */
  readonly tone?: "danger";
  /** Extra `data-*` attributes for the item's button (tests and capture kits find it by them). */
  readonly data?: Readonly<Record<`data-${string}`, string>>;
  readonly run: () => void;
}

/**
 * A button that opens a small floating list. Closes on a pick, Escape, or a click outside;
 * arrow keys move between items. `side` says which way the list opens from the trigger.
 */
export function Menu(props: {
  label: string;
  trigger: ReactNode;
  items: readonly MenuItem[];
  side?: "up" | "down";
  align?: "start" | "end";
  /** Extra content under the items (e.g. a segmented control). */
  footer?: ReactNode;
  triggerClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();
  const close = useCallback(() => setOpen(false), []);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open, close]);
  useEffect(() => {
    if (open) root.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [open]);
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
      root.current?.querySelector<HTMLElement>("[aria-haspopup]")?.focus();
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const items = [...(root.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];
    const at = items.indexOf(document.activeElement as HTMLElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    items[(at + step + items.length) % items.length]?.focus();
    event.preventDefault();
  };
  return (
    <div className="desk-menu-root" ref={root}>
      <button
        type="button"
        onKeyDown={onKeyDown}
        aria-label={props.label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        className={props.triggerClassName ?? "desk-icon-btn"}
        onClick={() => setOpen(!open)}
      >
        {props.trigger}
      </button>
      {open && (
        <div
          className="desk-menu"
          data-side={props.side ?? "down"}
          data-align={props.align ?? "start"}
        >
          <div id={listId} role="menu" aria-label={props.label} onKeyDown={onKeyDown}>
            {props.items.map((item) => (
              <button
                key={item.id}
                type="button"
                role="menuitem"
                tabIndex={-1}
                className="desk-menu-item"
                data-tone={item.tone}
                {...item.data}
                onClick={() => {
                  close();
                  item.run();
                }}
              >
                {item.icon}
                <span className="desk-menu-item-label">{item.label}</span>
                {item.hint !== undefined && <span className="desk-menu-hint">{item.hint}</span>}
              </button>
            ))}
          </div>
          {props.footer !== undefined && <div className="desk-menu-footer">{props.footer}</div>}
        </div>
      )}
    </div>
  );
}

// -- menu surface parts (W52-h-1) -----------------------------------------------------------------
// One family for every dropdown and context menu: a panel that owns arrow keys, Home/End, Escape
// and click-away, plus its row kinds. `MenuPanel` is positioned by the caller (`className` /
// `style`); `ContextMenuAt` places one at the pointer and keeps it inside the window.

const ROW_SELECTOR = '[role="menuitem"],[role="menuitemradio"],[role="menuitemcheckbox"]';

function rowsIn(root: HTMLElement | null): HTMLElement[] {
  if (root === null) return [];
  return [...root.querySelectorAll<HTMLElement>(ROW_SELECTOR)].filter(
    (row) => row.getAttribute("aria-disabled") !== "true",
  );
}

export function MenuPanel(props: {
  label: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  style?: React.CSSProperties;
  /** Focus the first row on open (off when a search field takes focus). */
  autoFocus?: boolean;
  /** Elements whose clicks do not count as "away" (the trigger). */
  keepOpenFor?: React.RefObject<HTMLElement | null>;
  id?: string;
}) {
  const root = useRef<HTMLDivElement>(null);
  const { onClose, keepOpenFor } = props;
  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (root.current?.contains(target)) return;
      if (keepOpenFor?.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [onClose, keepOpenFor]);
  useEffect(() => {
    if (props.autoFocus !== false) rowsIn(root.current)[0]?.focus();
  }, [props.autoFocus]);
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      event.preventDefault();
      onClose();
      return;
    }
    const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(event.key)) return;
    const rows = rowsIn(root.current);
    if (rows.length === 0) return;
    const at = rows.indexOf(document.activeElement as HTMLElement);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? rows.length - 1
          : (at + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
    rows[next]?.focus();
    event.preventDefault();
  };
  return (
    <div
      ref={root}
      id={props.id}
      role="menu"
      tabIndex={-1}
      aria-label={props.label}
      className={props.className === undefined ? "desk-menu" : `desk-menu ${props.className}`}
      style={props.style}
      onKeyDown={onKeyDown}
    >
      {props.children}
    </div>
  );
}

/** A menu at the pointer (right-click), kept inside the window. */
export function ContextMenuAt(props: {
  label: string;
  x: number;
  y: number;
  onClose: () => void;
  children: ReactNode;
}) {
  const [place, setPlace] = useState({ left: props.x, top: props.y });
  const probe = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const panel = probe.current?.firstElementChild as HTMLElement | null;
    if (panel === null || panel === undefined) return;
    const rect = panel.getBoundingClientRect();
    const left = Math.max(4, Math.min(props.x, window.innerWidth - rect.width - 4));
    const top = Math.max(4, Math.min(props.y, window.innerHeight - rect.height - 4));
    setPlace({ left, top });
  }, [props.x, props.y]);
  return (
    <div ref={probe} className="desk-context-layer">
      <MenuPanel
        label={props.label}
        onClose={props.onClose}
        className="desk-menu-at"
        style={{ left: place.left, top: place.top }}
      >
        {props.children}
      </MenuPanel>
    </div>
  );
}

/** The small uppercase label over a group of menu rows. */
export function MenuLabel(props: { children: ReactNode }) {
  return (
    <div className="desk-menu-label" role="presentation">
      {props.children}
    </div>
  );
}

export function MenuSeparator() {
  // biome-ignore lint/a11y/useSemanticElements: a menu separator inside an ARIA menu
  return <div className="desk-menu-sep" role="separator" />;
}

/**
 * One menu row: icon, label, optional key hint / check / switch / chevron. `kind` picks the role
 * (`radio` and `check` carry `aria-checked`); `tone="danger"` paints it red.
 */
export function MenuRow(props: {
  label: ReactNode;
  icon?: ReactNode;
  hint?: ReactNode;
  kind?: "item" | "radio" | "check" | "switch";
  checked?: boolean;
  tone?: "danger";
  disabled?: boolean;
  chevron?: boolean;
  ariaLabel?: string;
  onSelect: () => void;
  onMouseEnter?: () => void;
}) {
  const kind = props.kind ?? "item";
  const role =
    kind === "radio" ? "menuitemradio" : kind === "item" ? "menuitem" : "menuitemcheckbox";
  return (
    <button
      type="button"
      role={role}
      tabIndex={-1}
      aria-label={props.ariaLabel}
      {...(kind === "item" ? {} : { "aria-checked": props.checked === true })}
      aria-disabled={props.disabled ? "true" : undefined}
      className="desk-menu-item"
      data-tone={props.tone}
      onMouseEnter={props.onMouseEnter}
      onClick={() => {
        if (!props.disabled) props.onSelect();
      }}
    >
      {props.icon ?? null}
      <span className="desk-menu-item-label">{props.label}</span>
      {props.hint !== undefined && <span className="desk-menu-hint">{props.hint}</span>}
      {kind === "switch" && (
        <span className="desk-menu-switch" data-on={props.checked ? "true" : "false"} />
      )}
      {kind === "radio" && props.checked && (
        <Check aria-hidden="true" className="desk-menu-check" />
      )}
      {kind === "check" && props.checked && (
        <Check aria-hidden="true" className="desk-menu-check" />
      )}
      {props.chevron && <ChevronRight aria-hidden="true" className="desk-menu-chevron" />}
    </button>
  );
}

// -- floating HUD (palette, session switcher) ------------------------------------------------------

/**
 * A card centred at the top of the window, over a transparent click-catcher (no dimming). It is
 * outside the title band's drag region so clicks reach it.
 */
export function FloatingHud(props: {
  label: string;
  width: number;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  onKeyDown?: (event: React.KeyboardEvent) => void;
}) {
  return (
    <div className="desk-hud-layer">
      <button
        type="button"
        className="desk-hud-catcher"
        tabIndex={-1}
        aria-label={props.label}
        onClick={props.onClose}
      />
      <div
        className={props.className === undefined ? "desk-hud" : `desk-hud ${props.className}`}
        role="dialog"
        aria-modal="true"
        aria-label={props.label}
        style={{ width: props.width }}
        onKeyDown={props.onKeyDown}
      >
        {props.children}
      </div>
    </div>
  );
}

/** A shortcut accelerator (`CommandOrControl+Shift+N`) as key cap labels for this platform. */
export function keyCaps(accelerator: string, mac: boolean): string[] {
  const names: Record<string, string> = mac
    ? { CommandOrControl: "⌘", Command: "⌘", Control: "⌃", Shift: "⇧", Alt: "⌥" }
    : { CommandOrControl: "Ctrl", Command: "Ctrl", Control: "Ctrl", Shift: "Shift", Alt: "Alt" };
  return accelerator
    .split("+")
    .filter((part) => part !== "")
    .map((part) => names[part] ?? part);
}

// -- tooltip -------------------------------------------------------------------------------------

/**
 * Hover tip (inverted chip) shown after a short delay beside its anchor, in a fixed layer so a
 * scrolling list never clips it. Spread `bind` on the anchor and render `tip` anywhere.
 */
export function useHoverTip(
  text: string | null,
  side: "right" | "top" = "right",
): {
  bind: { onMouseEnter: (e: React.MouseEvent) => void; onMouseLeave: () => void };
  tip: ReactNode;
} {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  const bind = {
    onMouseEnter: (event: React.MouseEvent) => {
      if (text === null) return;
      const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        setAt(
          side === "right"
            ? { x: rect.right + 8, y: rect.top + rect.height / 2 }
            : { x: rect.left + rect.width / 2, y: rect.top - 8 },
        );
      }, TIP_DELAY_MS);
    },
    onMouseLeave: () => {
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
      setAt(null);
    },
  };
  const tip =
    at === null || text === null ? null : (
      <span className="desk-tip" data-side={side} role="tooltip" style={{ left: at.x, top: at.y }}>
        {text}
      </span>
    );
  return { bind, tip };
}

export const TIP_DELAY_MS = 400;

// -- small dialog --------------------------------------------------------------------------------

/**
 * A compact modal (≈448px) for one question or one short form: title, an optional line under it,
 * the body, and a right-aligned action row. Escape and the backdrop close it.
 */
export function SmallDialog(props: {
  title: string;
  description?: string;
  onClose: () => void;
  children?: ReactNode;
  actions?: ReactNode;
  /** `alertdialog` for a destructive confirm (Discard, Delete). */
  role?: "dialog" | "alertdialog";
  /** `wide`: the 512px form dialog of the overlays (W52-l: new cron, new subscription). */
  size?: "wide";
  /** Draw a close X in the top-right corner, named by this label. */
  closeLabel?: string;
}) {
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) props.onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="desk-dialog-backdrop" />
        <Dialog.Content
          className="desk-dialog"
          data-size={props.size}
          role={props.role ?? "dialog"}
          aria-describedby={undefined}
          onMouseDown={(event) => event.stopPropagation()}
        >
          <Dialog.Title className="desk-dialog-title">{props.title}</Dialog.Title>
          {props.description !== undefined && (
            <p className="desk-dialog-text">{props.description}</p>
          )}
          {props.children}
          {props.actions !== undefined && (
            <div className="desk-dialog-actions">{props.actions}</div>
          )}
          {props.closeLabel !== undefined && (
            <Dialog.Close className="desk-icon-btn desk-dialog-close" aria-label={props.closeLabel}>
              <X aria-hidden="true" />
            </Dialog.Close>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * A side panel's own title (W52-m): uppercase accent text after a small dithered square. `muted`
 * is the quiet variant for a panel's empty states ("NO FILES"). `title` is a native tooltip.
 */
export function PanelLabel(props: { children: ReactNode; muted?: boolean; title?: string }) {
  return (
    <span
      className="desk-panel-label"
      data-muted={props.muted === true ? "true" : undefined}
      title={props.title}
    >
      <span className="desk-dither" aria-hidden="true" />
      <span className="desk-panel-label-text">{props.children}</span>
    </span>
  );
}
