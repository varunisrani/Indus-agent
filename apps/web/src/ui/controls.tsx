// Shared dashboard controls (W51-a). Square corners, one ink colour, the type roles from index.css.
// docs/notes/ui-parity/web-foundation.md lists every component with its props and when to use it.

import { AlertCircle, ChevronDown, Inbox, LoaderCircle, RefreshCw, RotateCcw } from "lucide-react";
import type {
  ButtonHTMLAttributes,
  ComponentType,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { cx } from "./cx.ts";

/* ---------------------------------------------------------------- buttons */

type Tone = "default" | "primary" | "danger" | "ghost" | "danger-outline";
type Size = "sm" | "md" | "lg";

const toneClass: Record<Tone, string> = {
  default: "border-ink text-ink bg-transparent hover:bg-hover",
  primary: "border-ink bg-ink text-on-ink hover:opacity-90",
  danger: "border-danger bg-danger text-white hover:opacity-90",
  ghost: "border-transparent text-ink bg-transparent hover:bg-hover",
  /** Outlined in red: a destructive action that still asks first (Keys "Clear"); W52-d. */
  "danger-outline": "border-danger text-danger bg-transparent hover:bg-danger-wash",
};

const sizeClass: Record<Size, string> = {
  sm: "h-7 px-2.5 gap-1.5",
  md: "h-[1.875rem] px-3 gap-2",
  lg: "h-9 px-4 gap-2 text-[0.9375rem]",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** `default` is the outlined typewriter button most actions use. */
  tone?: Tone;
  size?: Size;
  /** A leading icon (lucide component instance), sized by the button. */
  icon?: ReactNode;
  /**
   * Capital letters, as the reference sets its buttons ("CREATE", "SWITCH"). On by default for every
   * tone but `ghost`; pass `false` for the few mixed-case buttons ("Show advanced"). W53 integration.
   */
  caps?: boolean;
}

export function Button({
  tone = "default",
  size = "md",
  icon,
  caps = tone !== "ghost",
  className = "",
  type = "button",
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cx(
        "type-button inline-flex shrink-0 items-center justify-center border whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-45 [&_svg]:size-3.5 [&_svg]:shrink-0",
        toneClass[tone],
        sizeClass[size],
        caps && "uppercase",
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
}

/** A square icon-only button; `label` is its accessible name and tooltip. */
export function IconButton({
  label,
  tone = "ghost",
  className = "",
  children,
  ...rest
}: Omit<ButtonProps, "icon" | "size"> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cx(
        "inline-flex size-7 shrink-0 items-center justify-center border transition-colors disabled:cursor-not-allowed disabled:opacity-45 [&_svg]:size-4",
        tone === "danger" ? "border-transparent text-danger hover:bg-danger-wash" : toneClass[tone],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

/* ---------------------------------------------------------------- badges and chips */

export type BadgeTone = "neutral" | "secondary" | "ok" | "warn" | "danger" | "accent";

const badgeClass: Record<BadgeTone, string> = {
  neutral: "border-line-strong text-ink-soft",
  /** Quiet filled chip: tags, tool names, counts in a card head, "Switched off", "default" (W52-c, -d, -e). */
  secondary: "border-line-strong bg-selected text-ink-muted",
  ok: "border-success/50 bg-success-wash text-success",
  warn: "border-warning/50 bg-warning-wash text-warning",
  danger: "border-danger/50 bg-danger-wash text-danger",
  accent: "border-ink text-ink",
};

/** Small condensed tag: a source, a state, a count. */
export function Badge({
  tone = "neutral",
  icon,
  children,
  className = "",
}: {
  tone?: BadgeTone;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "type-badge inline-flex h-6 shrink-0 items-center gap-1 border px-2 whitespace-nowrap [&_svg]:size-3",
        badgeClass[tone],
        className,
      )}
    >
      {icon}
      {children}
    </span>
  );
}

/** The boxed number next to a page title (`Sessions [3]`). */
export function CountChip({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <span
      {...(label ? { "aria-label": label } : {})}
      className="type-badge inline-flex h-6 min-w-6 items-center justify-center border border-line-strong bg-selected px-1.5 text-ink-muted"
    >
      {children}
    </span>
  );
}

/**
 * A spaced section heading inside a page, with an icon before it ("Scheduled Jobs (1)",
 * "Your MCP servers (0)"). Muted ink; use CardHeader for the head of a card instead (W52-c).
 */
export function SectionHeading({
  icon: Icon,
  children,
  id,
  level = 2,
}: {
  icon?: ComponentType<{ className?: string }>;
  children: ReactNode;
  id?: string;
  level?: 2 | 3;
}) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <Heading
      {...(id ? { id } : {})}
      className="type-heading m-0 flex items-center gap-2 text-ink-muted [&_svg]:size-4 [&_svg]:shrink-0"
    >
      {Icon ? <Icon aria-hidden="true" /> : null}
      <span className="min-w-0 truncate">{children}</span>
    </Heading>
  );
}

/* ---------------------------------------------------------------- segmented and tabs */

export interface SegmentOption<T extends string | number> {
  value: T;
  label: string;
}

/** Mutually exclusive filter buttons in one bordered strip; the picked one is filled with ink. */
export function Segmented<T extends string | number>({
  label,
  value,
  options,
  onChange,
  showLabel = false,
  fill = false,
}: {
  label: string;
  value: T;
  options: readonly SegmentOption<T>[];
  onChange: (next: T) => void;
  /** Show the legend before the strip (hidden by default, still read by screen readers). */
  showLabel?: boolean;
  /** Stretch the strip to the full width, options packed at the left (Cron Jobs/Blueprints). */
  fill?: boolean;
}) {
  return (
    <fieldset className="m-0 flex min-w-0 items-center gap-2 border-0 p-0">
      <legend className={showLabel ? "type-label float-left mr-2 text-ink-soft" : "sr-only"}>
        {label}
      </legend>
      <div className={cx("border border-line", fill ? "flex w-full" : "inline-flex")}>
        {options.map((option, i) => (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={option.value === value}
            onClick={() => onChange(option.value)}
            className={cx(
              "type-serif h-[1.875rem] px-3 text-[0.75rem] tracking-[0.08em] transition-colors",
              i > 0 && "border-l border-line",
              option.value === value ? "bg-ink text-on-ink" : "text-ink-muted hover:bg-hover",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

/** Page-level tabs (role="tablist"); the same strip look as Segmented. */
export function Tabs<T extends string>({
  label,
  value,
  tabs,
  onChange,
}: {
  label: string;
  value: T;
  tabs: readonly SegmentOption<T>[];
  onChange: (next: T) => void;
}) {
  return (
    <div role="tablist" aria-label={label} className="inline-flex border border-line">
      {tabs.map((tab, i) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          aria-selected={tab.value === value}
          onClick={() => onChange(tab.value)}
          className={cx(
            "type-serif h-[1.875rem] px-3 text-[0.75rem] tracking-[0.08em] transition-colors",
            i > 0 && "border-l border-line",
            tab.value === value ? "bg-ink text-on-ink" : "text-ink-muted hover:bg-hover",
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- form fields */

export function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="type-label text-ink-muted">
        {label}
      </label>
      {children}
      {hint ? <p className="type-serif text-[0.75rem] text-ink-soft">{hint}</p> : null}
    </div>
  );
}

/** Class for a text input, select or textarea when a page builds its own element. */
export const inputClass =
  "min-h-8 border border-line bg-transparent px-3 py-1 type-data text-ink placeholder:text-ink-faint outline-none transition-colors focus:border-ink disabled:opacity-50";

export function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx(inputClass, className)} {...rest} />;
}

export function Textarea({ className = "", ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(inputClass, "py-2", className)} {...rest} />;
}

/** Native select with the field look and a chevron. */
export function Select({
  className = "",
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className={cx("relative inline-flex", className)}>
      <select className={cx(inputClass, "w-full appearance-none pr-8")} {...rest}>
        {children}
      </select>
      <ChevronDown
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-ink-soft"
      />
    </span>
  );
}

/** A select dressed as an outlined typewriter button (filters such as "Any chat source"). */
export function SelectButton({
  icon: Icon,
  className = "",
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement> & { icon?: ComponentType<{ className?: string }> }) {
  return (
    <span className={cx("relative inline-flex text-ink", className)}>
      {Icon ? (
        <Icon className="pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2" />
      ) : null}
      <select
        className={cx(
          "type-button h-[1.875rem] w-full cursor-pointer appearance-none border border-ink bg-canvas pr-9 text-ink outline-none hover:bg-hover",
          Icon ? "pl-8" : "pl-3",
        )}
        {...rest}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 right-3 size-3.5 -translate-y-1/2"
      />
    </span>
  );
}

export function Checkbox({
  className = "",
  ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  return (
    <input
      type="checkbox"
      className={cx(
        "size-3.5 shrink-0 cursor-pointer appearance-none border border-line-strong bg-transparent checked:border-ink checked:bg-ink",
        className,
      )}
      {...rest}
    />
  );
}

/* ---------------------------------------------------------------- cards and lists */

export function Card({
  children,
  className = "",
  padded = true,
  ...rest
}: {
  children: ReactNode;
  className?: string;
  /** Inner padding; turn off when the card starts with a CardHeader. */
  padded?: boolean;
  "aria-label"?: string;
  /** Anchor for in-page jumps (Keys header chips); W52-d. */
  id?: string;
}) {
  return (
    <section className={cx("border border-line bg-canvas", padded && "p-4", className)} {...rest}>
      {children}
    </section>
  );
}

/** Card head: icon, wide uppercase title, and an aside for counts or actions. */
export function CardHeader({
  icon: Icon,
  title,
  aside,
  level = 2,
  subtitle,
  below,
}: {
  icon?: ComponentType<{ className?: string }>;
  title: ReactNode;
  aside?: ReactNode;
  level?: 2 | 3;
  /** A short muted note right after the title, in normal case ("applies to new sessions"). */
  subtitle?: ReactNode;
  /**
   * A line under the title: a chart legend (W52-b), or a plain string for the small serif count
   * line ("2 of 8 connected", W52-d), which also gives the header the roomier list-card padding.
   */
  below?: ReactNode;
}) {
  const Heading = level === 2 ? "h2" : "h3";
  if (typeof below === "string") {
    return (
      <header className="flex items-start gap-3 border-b border-line px-4 py-5 sm:px-6">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex min-w-0 items-center gap-2.5">
            {Icon ? <Icon className="size-5 shrink-0 text-ink-muted" /> : null}
            <Heading className="type-section-title m-0 truncate">{title}</Heading>
          </div>
          <p className="type-serif m-0 text-[0.75rem] text-ink-soft">{below}</p>
        </div>
        {aside ? <div className="flex shrink-0 items-center gap-2">{aside}</div> : null}
      </header>
    );
  }
  const titleRow = (
    <div className="flex min-w-0 flex-1 items-center gap-2.5">
      {Icon ? <Icon className="size-[1.125rem] shrink-0 text-ink" /> : null}
      <Heading
        className={cx("type-section-title m-0 truncate", subtitle === undefined && "flex-1")}
      >
        {title}
      </Heading>
      {subtitle === undefined ? null : (
        <span className="min-w-0 flex-1 truncate text-[0.75rem] text-ink-soft">{subtitle}</span>
      )}
    </div>
  );
  return (
    <header className="flex min-h-[3.375rem] items-center gap-2.5 border-b border-line px-4 py-2">
      {below === undefined ? (
        titleRow
      ) : (
        <div className="flex min-w-0 flex-1 flex-col gap-1.5 py-1.5">
          {titleRow}
          {below}
        </div>
      )}
      {aside ? <div className="flex shrink-0 items-center gap-2">{aside}</div> : null}
    </header>
  );
}

/** A bordered list row: content on the left, badges and actions on the right. */
export function ListRow({
  children,
  aside,
  className = "",
  ...rest
}: {
  children: ReactNode;
  aside?: ReactNode;
  className?: string;
  "data-testid"?: string;
}) {
  return (
    <li className={cx("flex items-start gap-3 border border-line px-3 py-3", className)} {...rest}>
      <div className="min-w-0 flex-1">{children}</div>
      {aside ? <div className="flex shrink-0 items-center gap-1.5">{aside}</div> : null}
    </li>
  );
}

export interface StatItem {
  label: string;
  value: ReactNode;
  tone?: "default" | "ok" | "warn" | "danger";
}

const statTone = {
  default: "text-ink",
  ok: "text-success",
  warn: "text-warning",
  danger: "text-danger",
} as const;

/** A row of figures in one bordered strip (value over label). */
export function StatStrip({ label, items }: { label: string; items: readonly StatItem[] }) {
  return (
    <dl
      aria-label={label}
      className="m-0 flex flex-wrap gap-x-6 gap-y-3 border border-line px-4 py-2.5"
    >
      {items.map((item) => (
        <div key={item.label} className="flex flex-col-reverse">
          <dt className="text-[0.75rem] text-ink-muted">{item.label}</dt>
          <dd className={cx("type-stat m-0", statTone[item.tone ?? "default"])}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Figures as a list with dotted leaders: value on the left, a dotted rule, the label spaced in
 * capitals on the right ("3.7K ........ TOTAL TOKENS"), as on the Analytics summary.
 */
export function LeaderStatList({ label, items }: { label: string; items: readonly StatItem[] }) {
  return (
    <dl aria-label={label} className="m-0 flex flex-col gap-4">
      {items.map((item) => (
        <div key={item.label} className="flex items-baseline gap-2">
          <dt className="flex min-w-0 flex-1 items-baseline gap-2 text-[0.9375rem] tracking-[0.2em] text-ink-muted uppercase">
            <span
              aria-hidden="true"
              className="min-w-6 flex-1 border-b border-dotted border-ink-faint"
            />
            <span className="shrink-0">{item.label}</span>
          </dt>
          <dd
            className={cx(
              "order-first m-0 shrink-0 font-display text-[0.875rem] [font-stretch:125%]",
              statTone[item.tone ?? "default"],
            )}
          >
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Label / value pairs in a grid (System host facts, detail panels). */
export function FactGrid({
  facts,
  columns = 3,
}: {
  facts: readonly { label: string; value: ReactNode }[];
  columns?: 2 | 3 | 4;
}) {
  const grid = { 2: "sm:grid-cols-2", 3: "sm:grid-cols-3", 4: "sm:grid-cols-4" }[columns];
  return (
    <dl className={cx("m-0 grid grid-cols-1 gap-x-6 gap-y-3", grid)}>
      {facts.map((fact) => (
        <div key={fact.label} className="flex flex-col">
          <dt className="text-[0.6875rem] tracking-[0.08em] text-ink-muted uppercase">
            {fact.label}
          </dt>
          <dd className="m-0 text-[0.875rem] text-ink">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ---------------------------------------------------------------- tables */

export const tableClass = "w-full border-collapse text-[0.8125rem]";
export const thClass =
  "type-serif h-10 border-b border-line px-0 text-left text-[0.75rem] font-normal text-ink-muted";
export const tdClass = "h-11 border-b border-line text-ink";

/* ---------------------------------------------------------------- states */

export function Notice({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "warn" | "danger";
  children: ReactNode;
}) {
  const colour =
    tone === "danger"
      ? "border-danger/70 text-danger"
      : tone === "warn"
        ? "border-warning/70 text-warning"
        : "border-line text-ink-muted";
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={cx("flex items-start gap-2 border px-3 py-2 text-[0.8125rem]", colour)}
    >
      {tone === "neutral" ? null : (
        <AlertCircle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      )}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/** A failed load: what failed in plain words, optional details, and a Retry button. */
export function ErrorState({
  message,
  details,
  onRetry,
}: {
  message: string;
  details?: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2 border border-danger/60 bg-danger/5 px-3 py-2"
    >
      <AlertCircle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-danger" />
      <div className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-[0.75rem] leading-snug">
        <p className="m-0 text-danger">{message}</p>
        {details ? <p className="m-0 break-all text-ink-muted">Details: {details}</p> : null}
        {onRetry ? (
          <Button size="sm" className="mt-1 h-6" icon={<RotateCcw />} onClick={onRetry}>
            Retry
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/** Nothing to show: a quiet icon and one line, centred. */
export function EmptyState({
  icon: Icon = Inbox,
  children,
  action,
}: {
  icon?: ComponentType<{ className?: string }>;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-14 text-center">
      <Icon aria-hidden="true" className="size-7 text-ink-soft" />
      <p className="m-0 text-[0.875rem] text-ink-muted">{children}</p>
      {action}
    </div>
  );
}

export function Loading({ children = "Loading…" }: { children?: ReactNode }) {
  return (
    <p className="m-0 flex items-center gap-2 text-[0.8125rem] text-ink-soft" aria-busy="true">
      <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />
      {children}
    </p>
  );
}

/** Loading / error / empty wrapper for one query. */
export function QueryState({
  loading,
  error,
  empty,
  emptyText,
  emptyIcon,
  onRetry,
  what,
  children,
}: {
  loading: boolean;
  error: unknown;
  empty?: boolean;
  emptyText?: string;
  emptyIcon?: ComponentType<{ className?: string }>;
  onRetry?: () => void;
  /** What failed to load ("the host numbers"); with `onRetry` the notice names it (W52-f-2). */
  what?: string;
  children: ReactNode;
}) {
  if (loading) return <Loading />;
  if (error) {
    const message = error instanceof Error ? error.message : "Something went wrong.";
    if (onRetry && what) {
      return (
        <ErrorState
          message={`Could not load ${what}. Check that the dashboard is running and press Retry.`}
          details={message}
          onRetry={onRetry}
        />
      );
    }
    return onRetry ? (
      <ErrorState message={message} onRetry={onRetry} />
    ) : (
      <Notice tone="danger">{message}</Notice>
    );
  }
  if (empty) {
    return (
      <EmptyState {...(emptyIcon ? { icon: emptyIcon } : {})}>
        {emptyText ?? "Nothing here yet."}
      </EmptyState>
    );
  }
  return <>{children}</>;
}

/* ---------------------------------------------------------------- header controls (W52-b) */

/**
 * Period picker for the header's after-title slot ("7d 30d 90d"): small square typewriter buttons,
 * the picked one filled with ink. `upper` sets the labels in capitals (Models).
 */
export function PeriodButtons<T extends number | string>({
  label = "Period",
  value,
  options,
  onChange,
  upper = false,
}: {
  label?: string;
  value: T;
  options: readonly SegmentOption<T>[];
  onChange: (next: T) => void;
  upper?: boolean;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a labelled button group, not a form fieldset
    <div role="group" aria-label={label} className="flex items-center gap-1.5">
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
          className={cx(
            "type-button h-7 min-w-[2.375rem] border border-ink px-2.5 text-[0.6875rem] transition-colors",
            upper && "uppercase",
            option.value === value ? "bg-ink text-on-ink" : "text-ink hover:bg-hover",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- list-page blocks (W52-d) */
// Added by wave 52 slice d (Channels, Webhooks, Pairing, Keys); any page with headed lists may use them.
// The list heading is SectionHeading above (one shared component, W52-c / W52-d).

/** An empty list: one bordered block, 80 px tall, with a single centred line. */
export function EmptyCard({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-20 items-center justify-center border border-line px-4 py-6 text-center text-[0.875rem] text-ink-muted">
      {children}
    </div>
  );
}

/** Ghost refresh icon (circular arrows) that turns into a spinner while `busy`. */
export function RefreshButton({
  onClick,
  busy = false,
  label = "Refresh",
}: {
  onClick: () => void;
  busy?: boolean;
  label?: string;
}) {
  return (
    <IconButton
      label={label}
      onClick={onClick}
      disabled={busy}
      aria-busy={busy}
      className="text-ink-soft hover:text-ink disabled:opacity-100"
    >
      {busy ? (
        <LoaderCircle aria-hidden="true" className="animate-spin" />
      ) : (
        <RefreshCw aria-hidden="true" />
      )}
    </IconButton>
  );
}

/** A page still loading its data: one centred spinner below the header. */
export function PageSpinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div aria-busy="true" className="flex justify-center pt-20 pb-8">
      <LoaderCircle aria-hidden="true" className="size-6 animate-spin text-ink-soft" />
      <span className="sr-only">{label}</span>
    </div>
  );
}

/** Inline code chip: ink fill, canvas text (paths, commands). `faded` dims it for secondary use. */
export function CodeChip({ children, faded = false }: { children: ReactNode; faded?: boolean }) {
  return (
    <code
      className={cx(
        "bg-ink px-1 font-mono text-[0.8125rem] break-all text-on-ink",
        faded && "opacity-60",
      )}
    >
      {children}
    </code>
  );
}

/**
 * A bordered notice block with an icon, text and an optional action on the right. `warning`
 * draws the border in the warning colour (restart needed, receiver off).
 */
export function Callout({
  tone = "neutral",
  icon: Icon,
  title,
  children,
  action,
  className = "",
  ...rest
}: {
  tone?: "neutral" | "warning";
  icon?: ComponentType<{ className?: string }>;
  title?: ReactNode;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
  role?: string;
  "data-testid"?: string;
}) {
  return (
    <div
      className={cx(
        "flex flex-wrap items-center gap-3 border px-4",
        tone === "warning" ? "border-warning/50" : "border-line",
        title ? "py-6" : "py-4",
        className,
      )}
      {...rest}
    >
      <div className="flex min-w-0 flex-1 items-start gap-3">
        {Icon ? (
          <Icon
            aria-hidden="true"
            className={cx(
              "shrink-0",
              title ? "size-5" : "mt-0.5 size-4",
              tone === "warning" ? "text-warning" : "text-ink-soft",
            )}
          />
        ) : null}
        <div className="min-w-0 flex-1 text-[0.875rem]">
          {title ? <p className="m-0 font-medium text-ink">{title}</p> : null}
          <div className={title ? "text-ink-muted" : "text-ink"}>{children}</div>
        </div>
      </div>
      {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
    </div>
  );
}
