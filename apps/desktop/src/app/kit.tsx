// The quiet controls shared by the app's pages (W52-k list-and-detail frame) and its overlay
// panels (W52-l): text actions, text tabs, the borderless search, uppercase captions, stat tiles,
// a log well that keeps to the bottom, list rows with a hover menu, definition rows, pills, a
// prompt block, a detail title row, a banner and dialog fields. One copy of each (W53 merge).
// Styles in kit.css on the `--desk-*` tokens.

import { AlertCircle, ArrowDownToLine, ArrowUpToLine, Copy, Ellipsis, Search } from "lucide-react";
import { type ReactNode, useLayoutEffect, useRef } from "react";
import { IconButton, Menu, type MenuItem } from "../ui/components.tsx";
import "./kit.css";

/** A quiet text action: 11px, underlined on hover; `strong` is semibold and always underlined. */
export function TextAction(props: {
  children: ReactNode;
  onClick: () => void;
  strong?: boolean;
  tone?: "danger";
  disabled?: boolean;
  /** Tooltip (e.g. why it is disabled). */
  title?: string;
}) {
  return (
    <button
      type="button"
      className="kit-text-action"
      data-strong={props.strong ? "true" : undefined}
      data-tone={props.tone}
      disabled={props.disabled}
      title={props.title}
      onClick={props.onClick}
    >
      {props.children}
    </button>
  );
}

/** An uppercase caption over a section ("RECENT LOGS", "DIAGNOSTICS"). */
export function CapsLabel(props: { children: ReactNode; aside?: ReactNode; id?: string }) {
  return (
    <div className="kit-caps-row">
      <h3 className="kit-caps" id={props.id}>
        {props.children}
      </h3>
      {props.aside !== undefined && <div className="kit-caps-aside">{props.aside}</div>}
    </div>
  );
}

export interface TextTabOption<T extends string> {
  readonly value: T;
  readonly label: string;
  /** A small raised count after the label; null = still loading (a tiny bar). */
  readonly count?: number | null | undefined;
}

/**
 * Plain text tabs with an underline on the current one and an optional raised count. `page`: the
 * 13px row centred in a page header (Capabilities, Artifacts); the default is the 12px panel row.
 */
export function TextTabs<T extends string>(props: {
  label: string;
  value: T;
  options: ReadonlyArray<TextTabOption<T>>;
  onChange: (value: T) => void;
  look?: "page";
}) {
  return (
    <div className="kit-text-tabs" data-look={props.look} role="tablist" aria-label={props.label}>
      {props.options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="tab"
          aria-selected={option.value === props.value}
          className="kit-text-tab"
          onClick={() => props.onChange(option.value)}
        >
          <span className="kit-tab-label">{option.label}</span>
          {option.count === null ? (
            <span className="kit-tab-count kit-tab-count-loading" aria-hidden="true" />
          ) : option.count !== undefined ? (
            <sup className="kit-tab-count">{option.count}</sup>
          ) : null}
        </button>
      ))}
    </div>
  );
}

/** The borderless search field with a magnifier (page header, list column, log filter). `fill`
 * stretches it over its cell; `width` fixes it. */
export function QuietSearch(props: {
  label: string;
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
  width?: number;
  fill?: boolean;
  className?: string;
}) {
  return (
    <label
      className={`desk-search kit-quiet-search${props.className === undefined ? "" : ` ${props.className}`}`}
      data-fill={props.fill ? "true" : undefined}
      style={props.width === undefined ? undefined : { width: props.width }}
    >
      <Search aria-hidden="true" size={14} />
      <input
        type="search"
        aria-label={props.label}
        placeholder={props.placeholder ?? props.label}
        value={props.value}
        onChange={(event) => props.onChange(event.currentTarget.value)}
      />
    </label>
  );
}

/** A label over a big number, with an optional hint line. */
export function StatTile(props: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="kit-stat">
      <span className="kit-stat-label">{props.label}</span>
      <span className="kit-stat-value">{props.value}</span>
      {props.hint !== undefined && <span className="kit-stat-hint">{props.hint}</span>}
    </div>
  );
}

function levelOf(line: string): "warn" | "error" | undefined {
  if (/\b(ERROR|CRITICAL)\b/.test(line)) return "error";
  if (/\b(WARNING|WARN)\b/.test(line)) return "warn";
  return undefined;
}

/** A tinted mono well of log lines that stays at the bottom unless the reader scrolled up;
 * warning and error lines carry a coloured rule in the gutter. */
export function LogTail(props: {
  lines: readonly string[];
  emptyText: string;
  labels: { top: string; bottom: string; copy: string };
  onCopy?: (text: string) => void;
  testId?: string;
  compact?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll again when the lines change
  useLayoutEffect(() => {
    const el = box.current;
    if (el !== null && pinned.current) el.scrollTop = el.scrollHeight;
  }, [props.lines]);
  const text = props.lines.join("\n");
  return (
    <div className="kit-log" data-compact={props.compact ? "true" : undefined}>
      <div
        className="kit-log-scroll"
        ref={box}
        onScroll={(event) => {
          const el = event.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
      >
        {props.lines.length === 0 ? (
          <p className="kit-log-empty">{props.emptyText}</p>
        ) : (
          <pre className="kit-log-lines" data-testid={props.testId}>
            {props.lines.map((line, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: log lines repeat; order is identity
              <span key={index} className="kit-log-line" data-level={levelOf(line)}>
                {line}
                {index < props.lines.length - 1 ? "\n" : ""}
              </span>
            ))}
          </pre>
        )}
      </div>
      {props.lines.length > 0 && (
        <div className="kit-log-tools">
          <IconButton
            label={props.labels.top}
            icon={<ArrowUpToLine aria-hidden="true" size={14} />}
            onClick={() => {
              pinned.current = false;
              box.current?.scrollTo({ top: 0 });
            }}
          />
          <IconButton
            label={props.labels.bottom}
            icon={<ArrowDownToLine aria-hidden="true" size={14} />}
            onClick={() => {
              pinned.current = true;
              const el = box.current;
              if (el !== null) el.scrollTop = el.scrollHeight;
            }}
          />
          {props.onCopy !== undefined && (
            <IconButton
              label={props.labels.copy}
              icon={<Copy aria-hidden="true" size={14} />}
              onClick={() => props.onCopy?.(text)}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** One row of a panel list: leading icon or status dot, title, quiet meta, hover "…" menu. */
export function PanelRow(props: {
  title: ReactNode;
  icon?: ReactNode;
  dot?: "on" | "off";
  meta?: ReactNode;
  selected?: boolean;
  onSelect: () => void;
  menu?: { label: string; items: readonly MenuItem[] };
  /** Extra `data-*` for tests. */
  data?: Readonly<Record<`data-${string}`, string>>;
}) {
  return (
    <li className="kit-row" data-selected={props.selected ? "true" : undefined} {...props.data}>
      <button
        type="button"
        className="kit-row-main"
        aria-current={props.selected ? "true" : undefined}
        onClick={props.onSelect}
      >
        {props.dot !== undefined && (
          <span className="kit-dot" data-state={props.dot} aria-hidden="true" />
        )}
        {props.icon !== undefined && <span className="kit-row-icon">{props.icon}</span>}
        <span className="kit-row-title">{props.title}</span>
        {props.meta !== undefined && <span className="kit-row-meta">{props.meta}</span>}
      </button>
      {props.menu !== undefined && props.menu.items.length > 0 && (
        <span className="kit-row-menu">
          <Menu
            label={props.menu.label}
            trigger={<Ellipsis aria-hidden="true" size={14} />}
            items={props.menu.items}
            align="end"
          />
        </span>
      )}
    </li>
  );
}

export type PillTone = "muted" | "category" | "accent" | "info" | "good" | "warn" | "bad";

/**
 * A small rounded tag. `muted` grey, `category` a deeper grey, `accent` the strong primary tint
 * ("In use"), `info` the soft primary tint ("Default"), `good` green, `warn` amber, `bad` red;
 * `dot` puts a status dot before the label.
 */
export function PanelPill(props: { children: ReactNode; tone?: PillTone; dot?: boolean }) {
  return (
    <span className="kit-pill" data-tone={props.tone ?? "muted"}>
      {props.dot && <span className="kit-pill-dot" aria-hidden="true" />}
      {props.children}
    </span>
  );
}

/** Two-column definition rows (80px label); `framed`: in an inset box (96px label). */
export function MetaGrid(props: {
  rows: ReadonlyArray<readonly [string, ReactNode]>;
  framed?: boolean;
}) {
  return (
    <dl className="kit-meta" data-framed={props.framed ? "true" : undefined}>
      {props.rows.map(([label, value]) => (
        <div key={label} className="kit-meta-row">
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A quiet pre-wrapped block (a prompt preview). */
export function PanelBlock(props: { children: ReactNode }) {
  return <div className="kit-block">{props.children}</div>;
}

/** The detail column's title row: optional leading tile, name, pills, actions on the right,
 * then an optional one-line description and anything `below`. `level` 2 on a page with no title. */
export function DetailHead(props: {
  title: ReactNode;
  pills?: ReactNode;
  actions?: ReactNode;
  below?: ReactNode;
  leading?: ReactNode;
  description?: ReactNode;
  level?: 2 | 3;
}) {
  const Heading = props.level === 2 ? "h2" : "h3";
  return (
    <header className="kit-detail-head">
      {props.leading !== undefined && <span className="kit-detail-leading">{props.leading}</span>}
      <div className="kit-detail-titles">
        <Heading className="kit-detail-title">
          {props.title}
          {props.pills}
        </Heading>
        {props.description !== undefined && props.description !== "" && (
          <p className="kit-detail-description">{props.description}</p>
        )}
        {props.below}
      </div>
      {props.actions !== undefined && <div className="kit-detail-actions">{props.actions}</div>}
    </header>
  );
}

/** A tinted, bordered banner with an icon, a title, a body and an action. */
export function Banner(props: {
  icon: ReactNode;
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="kit-banner" role="status">
      <span className="kit-banner-icon">{props.icon}</span>
      <div className="kit-banner-body">
        <p className="kit-banner-title">{props.title}</p>
        {props.children !== undefined && <div className="kit-banner-text">{props.children}</div>}
        {props.action !== undefined && <div className="kit-banner-action">{props.action}</div>}
      </div>
    </section>
  );
}

/** An inline error line (alert icon, danger text on a faint danger fill). */
export function InlineError(props: { children: ReactNode }) {
  return (
    <p className="kit-error" role="alert">
      <AlertCircle aria-hidden="true" size={14} />
      <span>{props.children}</span>
    </p>
  );
}

/** A dialog field: label (+ an optional quiet hint), then the control. */
export function Field(props: {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: ReactNode;
  help?: ReactNode;
}) {
  return (
    <div className="kit-field">
      <label className="kit-field-label" htmlFor={props.htmlFor}>
        {props.label}
        {props.hint !== undefined && <span className="kit-field-hint">{props.hint}</span>}
      </label>
      {props.children}
      {props.help !== undefined && <p className="kit-field-help">{props.help}</p>}
    </div>
  );
}
