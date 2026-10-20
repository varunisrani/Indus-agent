// Small shared controls for the dashboard pages, styled from the @indus/ui tokens.

import type { ButtonHTMLAttributes, ReactNode } from "react";

type Tone = "default" | "primary" | "danger" | "ghost";

const toneClass: Record<Tone, string> = {
  default: "border border-line bg-raised text-fg hover:bg-code",
  primary: "border border-accent bg-accent text-accent-fg hover:opacity-90",
  danger: "border border-danger bg-raised text-danger hover:bg-code",
  ghost: "border border-transparent text-fg hover:bg-code",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: Tone;
}

export function Button({
  tone = "default",
  className = "",
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60 ${toneClass[tone]} ${className}`}
      {...rest}
    />
  );
}

export type BadgeTone = "neutral" | "ok" | "warn" | "danger" | "accent";

const badgeClass: Record<BadgeTone, string> = {
  neutral: "border-line text-muted",
  ok: "border-ok text-ok",
  warn: "border-warn text-warn",
  danger: "border-danger text-danger",
  accent: "border-accent text-accent",
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 text-xs ${badgeClass[tone]}`}
    >
      {children}
    </span>
  );
}

export interface SegmentOption<T extends string | number> {
  value: T;
  label: string;
}

/** A row of mutually exclusive toggle buttons (filters, ranges). */
export function Segmented<T extends string | number>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly SegmentOption<T>[];
  onChange: (next: T) => void;
}) {
  return (
    <fieldset className="flex flex-wrap items-center gap-1 border-0 p-0">
      <legend className="mr-2 text-xs text-muted">{label}</legend>
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
          className={`rounded-md border px-2 py-1 text-xs ${
            option.value === value
              ? "border-accent bg-accent text-accent-fg"
              : "border-line bg-raised text-fg"
          }`}
        >
          {option.label}
        </button>
      ))}
    </fieldset>
  );
}

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
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="text-xs font-medium text-muted">
        {label}
      </label>
      {children}
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

export const inputClass =
  "rounded-md border border-line bg-raised px-2 py-1.5 text-sm text-fg placeholder:text-muted";

export function Notice({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "warn" | "danger";
  children: ReactNode;
}) {
  const colour =
    tone === "danger"
      ? "border-danger text-danger"
      : tone === "warn"
        ? "border-warn text-warn"
        : "border-line text-muted";
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={`rounded-md border px-3 py-2 text-sm ${colour}`}
    >
      {children}
    </div>
  );
}

/** Loading / error / empty wrapper for one query. */
export function QueryState({
  loading,
  error,
  empty,
  emptyText,
  children,
}: {
  loading: boolean;
  error: unknown;
  empty?: boolean;
  emptyText?: string;
  children: ReactNode;
}) {
  if (loading)
    return (
      <p className="text-sm text-muted" aria-busy="true">
        Loading…
      </p>
    );
  if (error) {
    const message = error instanceof Error ? error.message : "Something went wrong.";
    return <Notice tone="danger">{message}</Notice>;
  }
  if (empty) return <p className="text-sm text-muted">{emptyText ?? "Nothing here yet."}</p>;
  return <>{children}</>;
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-line bg-raised p-4 ${className}`}>
      {children}
    </section>
  );
}
