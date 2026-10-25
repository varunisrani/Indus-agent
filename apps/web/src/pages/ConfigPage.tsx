// Config (P18-20, re-laid out in W52-e): a section list beside one card of fields built from the
// config schema, a search in the header, a toolbar (export / import JSON, reset a section to the
// defaults, the YAML and Effective views, Save), an editable YAML view of what the file sets (Save
// sends it whole, W58-J2-2), and the effective view naming where each value comes from.
// `?section=<name>` opens on that section (W60-L1-2: the Models page's mixture-of-agents row).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Code,
  Download,
  RectangleEllipsis,
  RotateCcw,
  Search,
  SlidersHorizontal,
  Upload,
  X,
} from "lucide-react";
import {
  type ChangeEvent,
  type ComponentType,
  type ReactNode,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useSearchParams } from "react-router";
import { api } from "../lib/api.ts";
import { saveJson } from "../lib/format.ts";
import { getPath, setPath } from "../lib/nested.ts";
import type { ConfigObject, ConfigValue, ConfigField as FieldSchema } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, IconButton, Loading, QueryState } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { ConfigField, SubsectionDivider } from "./config/ConfigField.tsx";
import {
  categoryIcon,
  categoryName,
  fieldCategory,
  fieldMatches,
  orderedCategories,
} from "./config/categories.ts";
import { EffectiveView } from "./config/EffectiveView.tsx";
import { FilterColumn } from "./config/FilterColumn.tsx";
import { useYamlDraft, YamlView } from "./config/YamlView.tsx";

export const APPLY_NOTE = "Changes apply to the next session, or to the daemon after it restarts.";

type View = "form" | "yaml" | "effective";

/** Toolbar buttons are a touch shorter than the shell's (27 px). */
const toolClass = "h-[27px]";
/** A disabled Save keeps full-strength text on the muted fill. */
const saveClass =
  "h-[27px] disabled:border-transparent disabled:bg-selected disabled:text-ink disabled:opacity-100";

function isPlainObject(value: unknown): value is ConfigObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The second key segment of a three-plus segment key (its sub-section), else null. */
function subsection(key: string): string | null {
  const parts = key.split(".");
  return parts.length >= 3 ? (parts[1] ?? null) : null;
}

function HeaderSearch({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  return (
    <div className="relative w-[min(300px,50vw)]">
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-soft"
      />
      <input
        type="search"
        aria-label="Search settings"
        placeholder="Search..."
        value={value}
        onChange={(e) => onChange(e.currentTarget.value)}
        className="type-data h-[30px] w-full border border-line bg-transparent pr-8 pl-8 text-[0.75rem] text-ink outline-none placeholder:text-ink-faint focus:border-ink [&::-webkit-search-cancel-button]:hidden"
      />
      {value ? (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => onChange("")}
          className="absolute top-1/2 right-1.5 inline-flex size-5 -translate-y-1/2 items-center justify-center text-ink-soft hover:text-ink"
        >
          <X className="size-3.5" />
        </button>
      ) : null}
    </div>
  );
}

function SectionCard({
  icon: Icon,
  title,
  count,
  children,
}: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <Card padded={false} className="min-w-0 flex-1">
      <header className="flex h-12 items-center gap-2.5 border-b border-line px-4">
        <Icon aria-hidden="true" className="size-4 shrink-0 text-ink" />
        <h2 className="type-section-title m-0 flex-1 truncate text-[0.9375rem]">{title}</h2>
        <Badge tone="secondary">
          {count} {count === 1 ? "field" : "fields"}
        </Badge>
      </header>
      <div className="flex flex-col gap-2 px-4 pt-2 pb-4">{children}</div>
    </Card>
  );
}

export function ConfigPage() {
  const toast = useToast();
  const client = useQueryClient();
  const scope = useProfileScope();
  const [view, setView] = useState<View>("form");
  const [form, setForm] = useState<ConfigObject | null>(null);
  const [dirty, setDirty] = useState(false);
  const [query, setQuery] = useState("");
  const [params] = useSearchParams();
  const [picked, setPicked] = useState<string | null>(() => params.get("section"));
  const [resetting, setResetting] = useState(false);
  const config = useQuery({ queryKey: ["config", "file"], queryFn: () => api.config.get() });
  const schema = useQuery({ queryKey: ["config", "schema"], queryFn: () => api.config.schema() });
  const defaults = useQuery({
    queryKey: ["config", "defaults"],
    queryFn: () => api.config.defaults(),
    enabled: resetting,
  });

  useEffect(() => {
    if (config.data && form === null) setForm(config.data);
  }, [config.data, form]);
  // A saved YAML text replaced the file: the form reloads from it instead of keeping stale values.
  const draft = useYamlDraft(view === "yaml", () => {
    setForm(null);
    setDirty(false);
  });

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
  const categories = useMemo(
    () => (schema.data ? orderedCategories(schema.data) : []),
    [schema.data],
  );
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const field of Object.values(fields)) {
      const c = fieldCategory(field);
      out[c] = (out[c] ?? 0) + 1;
    }
    return out;
  }, [fields]);
  const searching = query.trim() !== "";
  const category = picked && categories.includes(picked) ? picked : (categories[0] ?? null);
  const scoped: [string, FieldSchema][] = Object.entries(fields).filter(([key, field]) =>
    searching ? fieldMatches(key, field, query) : fieldCategory(field) === category,
  );

  const resetToDefaults = () => {
    const tree = defaults.data;
    if (!tree) return;
    let next = form ?? {};
    let changed = 0;
    for (const [key] of scoped) {
      const value = getPath(tree, key);
      if (value === undefined) continue;
      next = setPath(next, key, value);
      changed += 1;
    }
    setForm(next);
    setDirty(true);
    setResetting(false);
    toast.show(
      `${changed} ${changed === 1 ? "field" : "fields"} set to the defaults. Save to keep them.`,
      "info",
    );
  };

  const profileName =
    scope.active ||
    scope.profiles.find((p) => p.is_current)?.name ||
    scope.defaultName ||
    "this profile";

  const renderFields = (rows: [string, FieldSchema][], grouped: boolean) => {
    if (!form) return null;
    const out: ReactNode[] = [];
    let lastGroup: string | null = null;
    let lastSub: string | null = null;
    for (const [key, field] of rows) {
      if (grouped) {
        const c = fieldCategory(field);
        if (c !== lastGroup) {
          const Icon = categoryIcon(c);
          out.push(
            <SubsectionDivider key={`group-${c}`}>
              <span className="inline-flex items-center gap-1.5">
                <Icon aria-hidden="true" className="size-3.5" />
                {categoryName(c)}
              </span>
            </SubsectionDivider>,
          );
          lastGroup = c;
          lastSub = null;
        }
      }
      const sub = subsection(key);
      if (sub && sub !== lastSub) {
        out.push(
          <SubsectionDivider key={`sub-${key}`}>{sub.replace(/_/g, " ")}</SubsectionDivider>,
        );
      }
      lastSub = sub;
      out.push(
        <ConfigField
          key={key}
          name={key}
          field={field}
          value={getPath(form, key)}
          onChange={(v) => edit(key, v)}
        />,
      );
    }
    return out;
  };

  const yaml = view === "yaml";
  return (
    <div className="flex flex-col gap-4 px-4 py-[1.4375rem] lg:px-[1.4375rem]">
      <HeaderActions>
        <HeaderSearch
          value={query}
          onChange={(next) => {
            setQuery(next);
            if (next.trim() && view !== "form") setView("form");
          }}
        />
      </HeaderActions>

      <div className="flex flex-wrap items-center gap-1.5">
        <SlidersHorizontal aria-hidden="true" className="size-4 shrink-0 text-ink-soft" />
        <code className="type-data mr-2 min-w-0 flex-1 bg-surface px-2 py-0.5 text-[0.8125rem] break-all text-ink-muted">
          config.yaml · {profileName}
        </code>
        <IconButton
          label="Export JSON"
          onClick={() => form && saveJson(form, "indus-config.json")}
          disabled={!form}
          className="size-[27px]"
        >
          <Download />
        </IconButton>
        <label
          title="Import JSON"
          className="inline-flex size-[27px] cursor-pointer items-center justify-center text-ink hover:bg-hover [&_svg]:size-4"
        >
          <Upload aria-hidden="true" />
          <span className="sr-only">Import JSON</span>
          <input
            type="file"
            accept="application/json,.json"
            className="sr-only"
            onChange={(e) => void importFile(e)}
          />
        </label>
        {view === "form" ? (
          <IconButton
            label={searching ? "Reset these results to defaults" : "Reset section to defaults"}
            onClick={() => setResetting(true)}
            disabled={!form || scoped.length === 0}
            className="size-[27px]"
          >
            <RotateCcw />
          </IconButton>
        ) : null}
        <span aria-hidden="true" className="mx-1 h-5 w-px bg-line" />
        <Button
          size="sm"
          tone={yaml ? "primary" : "default"}
          className={toolClass}
          icon={yaml ? <RectangleEllipsis /> : <Code />}
          onClick={() => setView(yaml ? "form" : "yaml")}
        >
          {yaml ? "Form" : "YAML"}
        </Button>
        <Button
          size="sm"
          tone={view === "effective" ? "primary" : "default"}
          className={toolClass}
          aria-pressed={view === "effective"}
          onClick={() => setView(view === "effective" ? "form" : "effective")}
        >
          Effective
        </Button>
        <Button
          size="sm"
          tone="primary"
          className={saveClass}
          disabled={yaml ? !draft.dirty || draft.saving : !dirty || !form || save.isPending}
          onClick={() => (yaml ? draft.save() : form && save.mutate(form))}
        >
          Save
        </Button>
      </div>

      {view === "effective" ? <EffectiveView /> : null}
      {yaml ? <YamlView draft={draft} /> : null}
      {view === "form" ? (
        config.isPending || schema.isPending || (form === null && !config.error) ? (
          <div className="flex justify-center py-24">
            <Loading />
          </div>
        ) : (
          <QueryState
            loading={false}
            error={config.error ?? schema.error}
            onRetry={() => {
              void config.refetch();
              void schema.refetch();
            }}
          >
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
              <FilterColumn
                categories={categories}
                counts={counts}
                active={searching ? null : category}
                onPick={(next) => {
                  setQuery("");
                  setPicked(next);
                }}
              />
              {searching ? (
                <SectionCard icon={Search} title="Search results" count={scoped.length}>
                  {scoped.length ? (
                    renderFields(scoped, true)
                  ) : (
                    <p className="m-0 py-10 text-center text-[0.8125rem] text-ink-soft">
                      No fields match '{query.trim()}'
                    </p>
                  )}
                </SectionCard>
              ) : category ? (
                <SectionCard
                  icon={categoryIcon(category)}
                  title={categoryName(category)}
                  count={scoped.length}
                >
                  {renderFields(scoped, false)}
                </SectionCard>
              ) : (
                <Card className="flex-1">
                  <p className="m-0 text-[0.8125rem] text-ink-soft">
                    The schema lists no settings.
                  </p>
                </Card>
              )}
            </div>
          </QueryState>
        )
      ) : null}

      <ConfirmDialog
        open={resetting}
        title="Reset to defaults?"
        description={
          searching
            ? `The ${scoped.length} matching fields go back to their built-in values in the form. Nothing is written until you save.`
            : `The ${scoped.length} fields of ${category ? categoryName(category) : "this section"} go back to their built-in values in the form. Nothing is written until you save.`
        }
        confirmLabel="Reset to defaults"
        danger
        busy={defaults.isPending || !defaults.data}
        onConfirm={resetToDefaults}
        onCancel={() => setResetting(false)}
      >
        {defaults.error ? (
          <p className={cx("m-0 text-[0.8125rem] text-danger")}>{errorText(defaults.error)}</p>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}
