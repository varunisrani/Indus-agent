// One config setting in the form: a serif uppercase label, the key path and description as hints,
// and the control its schema type asks for. Every control carries the full key as its name.

import { type ReactNode, useEffect, useId, useState } from "react";
import type { ConfigValue, ConfigField as FieldSchema } from "../../lib/restTypes.ts";
import { inputClass, Select } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { Switch } from "../../ui/Switch.tsx";
import { fieldLabel } from "./categories.ts";

const controlClass = cx(inputClass, "h-[34px] w-full text-[0.8125rem]");

export function displayValue(value: ConfigValue | undefined): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function Hints({ name, description }: { name: string; description: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      {name.includes(".") ? (
        <span className="type-data text-[0.75rem] text-ink-soft">{name}</span>
      ) : null}
      {description ? (
        <span className="text-[0.75rem] leading-snug text-ink-muted">{description}</span>
      ) : null}
    </div>
  );
}

function splitCommas(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

/** Scalars as one comma-separated input; the text is kept while typing ("a, " stays). */
function CommaList({
  id,
  name,
  value,
  onChange,
}: {
  id: string;
  name: string;
  value: ConfigValue[];
  onChange: (next: ConfigValue) => void;
}) {
  const joined = value.map(displayValue).join(", ");
  const [text, setText] = useState(joined);
  useEffect(() => {
    setText((current) => (splitCommas(current).join(", ") === joined ? current : joined));
  }, [joined]);
  return (
    <input
      id={id}
      aria-label={name}
      className={controlClass}
      placeholder="comma-separated values"
      value={text}
      onChange={(e) => {
        const next = e.currentTarget.value;
        setText(next);
        onChange(splitCommas(next));
      }}
    />
  );
}

/** A list holding objects: one JSON value per line; a bad line is reported and not applied. */
function JsonLines({
  id,
  name,
  value,
  onChange,
}: {
  id: string;
  name: string;
  value: ConfigValue[];
  onChange: (next: ConfigValue) => void;
}) {
  const joined = value.map((item) => JSON.stringify(item)).join("\n");
  const [text, setText] = useState(joined);
  const [bad, setBad] = useState<number | null>(null);
  useEffect(() => {
    setText((current) => (bad === null ? joined : current));
  }, [joined, bad]);
  return (
    <>
      <textarea
        id={id}
        aria-label={name}
        aria-invalid={bad !== null}
        rows={Math.min(Math.max(value.length, 2), 8)}
        className={cx(inputClass, "w-full py-2 text-[0.8125rem]")}
        value={text}
        onChange={(e) => {
          const next = e.currentTarget.value;
          setText(next);
          const parsed: ConfigValue[] = [];
          const lines = next.split("\n");
          for (const [i, line] of lines.entries()) {
            if (!line.trim()) continue;
            try {
              parsed.push(JSON.parse(line) as ConfigValue);
            } catch {
              setBad(i + 1);
              return;
            }
          }
          setBad(null);
          onChange(parsed);
        }}
      />
      {bad !== null ? (
        <span className="text-[0.75rem] text-danger">Line {bad} is not valid JSON.</span>
      ) : null}
    </>
  );
}

export function ConfigField({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: FieldSchema;
  value: ConfigValue | undefined;
  onChange: (next: ConfigValue) => void;
}) {
  const id = useId();
  const label = (
    <label htmlFor={id} className="type-label text-[0.75rem] tracking-[0.08em] text-ink">
      {fieldLabel(name)}
    </label>
  );

  if (field.type === "boolean") {
    return (
      <div className="flex items-center justify-between gap-4 py-1">
        <div className="flex min-w-0 flex-col gap-1">
          {label}
          <Hints name={name} description={field.description} />
        </div>
        <Switch id={id} checked={value === true} onCheckedChange={onChange} label={name} />
      </div>
    );
  }

  let control: ReactNode;
  if (field.type === "enum") {
    control = (
      <Select
        id={id}
        aria-label={name}
        className="w-full [&_select]:h-[34px] [&_select]:text-[0.8125rem]"
        value={displayValue(value)}
        onChange={(e) => onChange(e.currentTarget.value)}
      >
        {value === undefined || value === null ? <option value="">(not set)</option> : null}
        {(field.options ?? []).map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </Select>
    );
  } else if (field.type === "number" || field.type === "integer") {
    control = (
      <input
        id={id}
        aria-label={name}
        type="number"
        step={field.type === "integer" ? 1 : "any"}
        className={controlClass}
        value={displayValue(value)}
        onChange={(e) => {
          const raw = e.currentTarget.value;
          const parsed = Number(raw);
          onChange(raw === "" ? null : Number.isFinite(parsed) ? parsed : raw);
        }}
      />
    );
  } else if (field.type === "list") {
    const items = Array.isArray(value) ? value : [];
    const hasObjects = items.some((item) => typeof item === "object" && item !== null);
    control = hasObjects ? (
      <JsonLines id={id} name={name} value={items} onChange={onChange} />
    ) : (
      <CommaList id={id} name={name} value={items} onChange={onChange} />
    );
  } else {
    control = (
      <input
        id={id}
        aria-label={name}
        type="text"
        className={controlClass}
        value={displayValue(value)}
        onChange={(e) => onChange(e.currentTarget.value)}
      />
    );
  }

  return (
    <div className="flex flex-col gap-1.5 py-1.5">
      {label}
      <Hints name={name} description={field.description} />
      {control}
    </div>
  );
}

/** The row between groups of keys: a small serif caption and a rule filling the width. */
export function SubsectionDivider({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-3 pt-4 pb-2">
      <span className="type-label text-[0.6875rem] font-semibold text-ink-muted">{children}</span>
      <span aria-hidden="true" className="h-px flex-1 bg-line" />
    </div>
  );
}
