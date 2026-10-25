// The shared full-window overlay card (W51-b) and the two frames that sit in it:
//
// - `OverlayCard`: a modal card inset from the window edges over a dimmed backdrop, with the close
//   X on its title line. Escape and the backdrop close it (Radix Dialog).
// - `NavFrame`: a tinted 208px left nav beside a main column headed by a `Breadcrumb`
//   (Settings, Command center).
// - `ListDetail`: a header with a count, a searchable 208px list with a "+" row, and a detail
//   column that shows a centred empty state until something is chosen (Cron, Profiles, Webhooks,
//   Agents, Learning map).
//
// Small parts used inside them: `TextLink`, `Segmented`, `TwoLineRow`. Styles in overlay.css.

import * as Dialog from "@radix-ui/react-dialog";
import { ChevronRight, Plus, X } from "lucide-react";
import type { ReactNode } from "react";
import { EmptyState } from "../../ui/components.tsx";
import { QuietSearch } from "../kit.tsx";
import "./overlay.css";

export function OverlayCard(props: {
  /** The dialog's accessible name (shown by the frame's breadcrumb or header, or hidden). */
  title: string;
  closeLabel: string;
  onClose: () => void;
  children: ReactNode;
  /** Shown centred on the card's top edge (e.g. a search pill). */
  top?: ReactNode;
  className?: string;
  /** Icon buttons left of the X on the title line (W52-l: session import's refresh). */
  titlebarActions?: ReactNode;
  /** `elevated`: the lighter raised surface (session import). */
  surface?: "default" | "elevated";
  /** Names the overlay on the card (`data-overlay`) for tests and the capture kit. */
  id?: string;
}) {
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) props.onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="overlay-backdrop" />
        <Dialog.Content
          className={
            props.className === undefined
              ? "desk-overlay-card"
              : `desk-overlay-card ${props.className}`
          }
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            // W52-l: open with the card focused (no focus ring on its first control)
            event.preventDefault();
            (event.currentTarget as HTMLElement | null)?.focus();
          }}
          data-overlay={props.id}
          data-surface={props.surface === "elevated" ? "elevated" : undefined}
        >
          <Dialog.Title className="sr-only">{props.title}</Dialog.Title>
          {props.top !== undefined && <div className="desk-overlay-top">{props.top}</div>}
          {props.children}
          <div className="desk-overlay-titlebar">
            {props.titlebarActions}
            <Dialog.Close
              className="desk-icon-btn desk-overlay-close"
              aria-label={props.closeLabel}
            >
              <X aria-hidden="true" />
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export interface Crumb {
  readonly label: string;
  /** Absent on the last crumb (the page you are on). */
  readonly onSelect?: () => void;
}

export function Breadcrumb(props: { label: string; crumbs: readonly Crumb[] }) {
  return (
    <ol className="desk-breadcrumb" aria-label={props.label}>
      {props.crumbs.map((crumb, index) => {
        const last = index === props.crumbs.length - 1;
        return (
          <li key={crumb.label} aria-current={last ? "page" : undefined}>
            {index > 0 && <ChevronRight aria-hidden="true" />}
            {crumb.onSelect !== undefined && !last ? (
              <button type="button" className="desk-crumb" onClick={crumb.onSelect}>
                {crumb.label}
              </button>
            ) : (
              <span>{crumb.label}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** Left nav (208px, tinted) beside the main column. */
export function NavFrame(props: {
  nav: ReactNode;
  navLabel: string;
  header?: ReactNode;
  children: ReactNode;
  navFooter?: ReactNode;
}) {
  return (
    <div className="desk-nav-frame">
      <nav className="desk-overlay-nav" aria-label={props.navLabel}>
        <div className="desk-overlay-nav-body">{props.nav}</div>
        {props.navFooter !== undefined && (
          <div className="desk-overlay-nav-footer">{props.navFooter}</div>
        )}
      </nav>
      <section className="desk-overlay-main">
        {props.header !== undefined && (
          <header className="desk-overlay-head">{props.header}</header>
        )}
        <div className="desk-overlay-body">{props.children}</div>
      </section>
    </div>
  );
}

/** One entry of a `NavFrame` nav: icon, label, optional chevron; nested pages go in `children`. */
export function NavEntry(props: {
  icon?: ReactNode;
  label: string;
  current?: boolean;
  expanded?: boolean;
  /** Draw a chevron (a group with pages). */
  group?: boolean;
  depth?: 0 | 1;
  onSelect: () => void;
  children?: ReactNode;
}) {
  return (
    <li className="desk-nav-entry" data-depth={props.depth ?? 0}>
      <button
        type="button"
        className="desk-nav-link"
        aria-current={props.current ? "page" : undefined}
        aria-expanded={props.group ? props.expanded === true : undefined}
        onClick={props.onSelect}
      >
        {props.icon}
        <span className="desk-nav-label">{props.label}</span>
        {props.group && <ChevronRight aria-hidden="true" className="desk-nav-caret" />}
      </button>
      {props.children}
    </li>
  );
}

/** Master/detail (the reference's panel, W52-l): an optional header (title, quiet subtitle,
 * actions), a 208px list column (borderless search, rows, a "+" row, an optional footer such as
 * a section of blueprints) and a detail column that shows a centred empty state until a row is
 * chosen. With `whole` the list column is dropped and the empty state fills the body. */
export function ListDetail(props: {
  title?: string;
  /** Quiet line under the title ("3 jobs"). */
  subtitle?: string;
  count?: number;
  actions?: ReactNode;
  search?: {
    label: string;
    placeholder?: string;
    value: string;
    onChange: (value: string) => void;
  };
  add?: { label: string; onAdd: () => void; disabled?: boolean };
  list: ReactNode;
  listLabel: string;
  /** Under the rows, above the "+" row (e.g. "No scheduled jobs yet"). */
  listEmpty?: ReactNode;
  /** Under the "+" row (e.g. the blueprints section). */
  listFooter?: ReactNode;
  /** Above the list (e.g. the "All profiles" toggle). */
  listTop?: ReactNode;
  /** Full-width content above the columns (banners). */
  banner?: ReactNode;
  detail?: ReactNode;
  empty: { icon?: ReactNode; title: ReactNode; description?: ReactNode; action?: ReactNode };
  /** Show only the empty state across the whole body (nothing to list). */
  whole?: boolean;
}) {
  const empty = (
    <div className="desk-detail-empty">
      <EmptyState look="panel" {...props.empty} />
    </div>
  );
  return (
    <div className="desk-list-detail">
      {props.title !== undefined && (
        <header className="desk-list-detail-head">
          <div className="desk-list-detail-titles">
            <h2>
              {props.title}
              {props.count !== undefined && <span className="desk-count">{props.count}</span>}
            </h2>
            {props.subtitle !== undefined && (
              <p className="desk-list-detail-subtitle">{props.subtitle}</p>
            )}
          </div>
          {props.actions !== undefined && (
            <div className="desk-list-detail-actions">{props.actions}</div>
          )}
        </header>
      )}
      {props.banner}
      {props.whole === true ? (
        <div className="desk-list-detail-whole">{empty}</div>
      ) : (
        <div className="desk-list-detail-cols">
          <div className="desk-list-col">
            {props.search !== undefined && (
              <QuietSearch className="desk-list-search" {...props.search} />
            )}
            {props.listTop}
            <ul className="desk-list" aria-label={props.listLabel}>
              {props.list}
            </ul>
            {props.listEmpty}
            {props.add !== undefined && (
              <button
                type="button"
                className="desk-add-row"
                aria-label={props.add.label}
                title={props.add.label}
                disabled={props.add.disabled}
                onClick={props.add.onAdd}
              >
                <Plus aria-hidden="true" size={14} />
              </button>
            )}
            {props.listFooter}
          </div>
          <div className="desk-detail-col">{props.detail ?? empty}</div>
        </div>
      )}
    </div>
  );
}

/** A list row with a title line and a quieter second line. */
export function TwoLineRow(props: {
  title: ReactNode;
  detail?: ReactNode;
  meta?: ReactNode;
  selected?: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        className="desk-two-line"
        aria-current={props.selected ? "true" : undefined}
        onClick={props.onSelect}
      >
        <span className="desk-two-line-title">{props.title}</span>
        {props.meta !== undefined && <span className="desk-two-line-meta">{props.meta}</span>}
        {props.detail !== undefined && <span className="desk-two-line-detail">{props.detail}</span>}
      </button>
    </li>
  );
}

/** An underlined text action ("Open logs", "Change"). */
export function TextLink(props: {
  children: ReactNode;
  onClick: () => void;
  tone?: "default" | "danger";
}) {
  return (
    <button
      type="button"
      className="desk-text-link"
      data-tone={props.tone ?? "default"}
      onClick={props.onClick}
    >
      {props.children}
    </button>
  );
}

/** A small segmented control for 2–4 mutually exclusive choices. */
export function Segmented<T extends string>(props: {
  label: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string; icon?: ReactNode }>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="desk-segmented" role="radiogroup" aria-label={props.label}>
      {props.options.map((option) => (
        // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={option.value === props.value}
          className="desk-segment"
          onClick={() => props.onChange(option.value)}
        >
          {option.icon}
          {option.label}
        </button>
      ))}
    </div>
  );
}
