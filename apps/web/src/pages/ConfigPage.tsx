// Config (P18-20): a form built from the config schema, grouped by category, plus a read-only
// "effective" view naming where each value comes from.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, useEffect, useId, useState } from "react";
import { api } from "../lib/api.ts";
import { saveJson } from "../lib/format.ts";
import { getPath, setPath } from "../lib/nested.ts";
import type { ConfigField, ConfigObject, ConfigValue, ValueSource } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  type BadgeTone,
  Button,
  Card,
  Field,
  inputClass,
  Notice,
  QueryState,
  Segmented,
} from "../ui/controls.tsx";
import { Switch } from "../ui/Switch.tsx";

export const APPLY_NOTE = "Changes apply to the next session, or to the daemon after it restarts.";

type View = "form" | "effective";

function isPlainObject(value: unknown): value is ConfigObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function displayValue(value: ConfigValue | undefined): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function FieldControl({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: ConfigField;
  value: ConfigValue | undefined;
  onChange: (next: ConfigValue) => void;
}) {
  const id = useId();
  const label = name.split(".").pop() ?? name;
  if (field.type === "boolean") {
    return (
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col">
          <span className="text-sm">{label}</span>
          <span className="text-xs text-muted">{field.description}</span>
        </div>
        <Switch checked={value === true} onCheckedChange={(next) => onChange(next)} label={name} />
      </div>
    );
  }
  if (field.type === "enum") {
    return (
      <Field label={label} htmlFor={id} hint={field.description}>
        <select
          id={id}
          aria-label={name}
          className={inputClass}
          value={displayValue(value)}
          onChange={(e) => onChange(e.currentTarget.value)}
        >
          {value === undefined || value === null ? <option value="">(not set)</option> : null}
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </Field>
    );
  }
  if (field.type === "number" || field.type === "integer") {
    return (
      <Field label={label} htmlFor={id} hint={field.description}>
        <input
          id={id}
          aria-label={name}
          type="number"
          step={field.type === "integer" ? 1 : "any"}
          className={inputClass}
          value={displayValue(value)}
          onChange={(e) => {
            const raw = e.currentTarget.value;
            const parsed = Number(raw);
            onChange(raw === "" ? null : Number.isFinite(parsed) ? parsed : raw);
          }}
        />
      </Field>
    );
  }
  if (field.type === "list") {
    const lines = Array.isArray(value) ? value.map(displayValue).join("\n") : displayValue(value);
    return (
      <Field label={label} htmlFor={id} hint={`${field.description} One entry per line.`}>
        <textarea
          id={id}
          aria-label={name}
          className={inputClass}
          rows={3}
          value={lines}
          onChange={(e) =>
            onChange(e.currentTarget.value.split("\n").filter((line) => line.trim() !== ""))
          }
        />
      </Field>
    );
  }
  return (
    <Field label={label} htmlFor={id} hint={field.description}>
      <input
        id={id}
        aria-label={name}
        type="text"
        className={inputClass}
        value={displayValue(value)}
        onChange={(e) => onChange(e.currentTarget.value)}
      />
    </Field>
  );
}

const SOURCE_TONE: Record<ValueSource, BadgeTone> = {
  default: "neutral",
  file: "accent",
  env: "warn",
  overlay: "danger",
};

const SOURCE_LABEL: Record<ValueSource, string> = {
  default: "default",
  file: "config file",
  env: "environment",
  overlay: "admin overlay",
};

function EffectiveView() {
  const effective = useQuery({
    queryKey: ["config", "effective"],
    queryFn: () => api.config.effective(),
  });
  const entries = effective.data?.entries ?? [];
  return (
    <QueryState
      loading={effective.isPending}
      error={effective.error}
      empty={entries.length === 0}
      emptyText="No settings reported."
    >
      <table className="w-full text-sm" aria-label="Effective configuration">
        <thead>
          <tr className="text-left text-xs text-muted">
            <th className="py-1">Key</th>
            <th className="py-1">Value</th>
            <th className="py-1">Source</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr
              key={entry.key}
              className="border-t border-line"
              data-testid={`effective-${entry.key}`}
            >
              <td className="py-1 font-mono text-xs">{entry.key}</td>
              <td className="py-1 font-mono text-xs break-all">{displayValue(entry.value)}</td>
              <td className="py-1">
                <Badge tone={SOURCE_TONE[entry.source]}>{SOURCE_LABEL[entry.source]}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </QueryState>
  );
}

export function ConfigPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [view, setView] = useState<View>("form");
  const [form, setForm] = useState<ConfigObject | null>(null);
  const [dirty, setDirty] = useState(false);
  const config = useQuery({ queryKey: ["config", "file"], queryFn: () => api.config.get() });
  const schema = useQuery({ queryKey: ["config", "schema"], queryFn: () => api.config.schema() });

  useEffect(() => {
    if (config.data && form === null) setForm(config.data);
  }, [config.data, form]);

  const save = useMutation({
    mutationFn: (next: ConfigObject) => api.config.put(next),
    onSuccess: () => {
      setDirty(false);
      toast.show(`Saved. ${APPLY_NOTE}`, "success");
      void client.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const edit = (key: string, value: ConfigValue) => {
    setForm((current) => setPath(current ?? {}, key, value));
    setDirty(true);
  };

  const importFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    try {
      const parsed: unknown = JSON.parse(await file.text());
      if (!isPlainObject(parsed)) throw new Error("not an object");
      setForm(parsed);
      setDirty(true);
      toast.show("Imported into the form. Save to keep it.", "info");
    } catch {
      toast.show("That file is not a JSON config object.", "error");
    }
  };

  const fields = schema.data?.fields ?? {};
  const order = schema.data?.category_order ?? [];
  const categories = [...new Set(Object.values(fields).map((f) => f.category || "general"))].sort(
    (a, b) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      return (ia < 0 ? order.length : ia) - (ib < 0 ? order.length : ib) || a.localeCompare(b);
    },
  );

  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Button onClick={() => form && saveJson(form, "indus-config.json")} disabled={!form}>
          Export JSON
        </Button>
        <label className="inline-flex cursor-pointer items-center rounded-md border border-line bg-raised px-3 py-1.5 text-sm">
          Import JSON
          <input
            type="file"
            accept="application/json,.json"
            className="sr-only"
            onChange={(e) => void importFile(e)}
          />
        </label>
      </HeaderActions>
      <Notice>{APPLY_NOTE}</Notice>
      <Segmented
        label="View"
        value={view}
        options={[
          { value: "form", label: "Settings" },
          { value: "effective", label: "Effective" },
        ]}
        onChange={setView}
      />
      {view === "effective" ? (
        <EffectiveView />
      ) : (
        <QueryState
          loading={config.isPending || schema.isPending || form === null}
          error={config.error ?? schema.error}
        >
          {form ? (
            <form
              className="flex flex-col gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                save.mutate(form);
              }}
            >
              {categories.map((category) => (
                <Card key={category}>
                  <h2 className="mb-3 text-sm font-semibold capitalize">{category}</h2>
                  <div className="flex flex-col gap-3">
                    {Object.entries(fields)
                      .filter(([, field]) => (field.category || "general") === category)
                      .map(([name, field]) => (
                        <FieldControl
                          key={name}
                          name={name}
                          field={field}
                          value={getPath(form, name)}
                          onChange={(v) => edit(name, v)}
                        />
                      ))}
                  </div>
                </Card>
              ))}
              <div className="flex gap-2">
                <Button type="submit" tone="primary" disabled={!dirty || save.isPending}>
                  Save
                </Button>
                <Button
                  disabled={!dirty}
                  onClick={() => {
                    setForm(config.data ?? {});
                    setDirty(false);
                  }}
                >
                  Reset
                </Button>
              </div>
            </form>
          ) : null}
        </QueryState>
      )}
    </div>
  );
}
