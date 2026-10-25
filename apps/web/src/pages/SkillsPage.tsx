// Skills (P18-22, look W52-c): a filter panel (All, Toolsets, Browse hub, and categories) beside
// the chosen view. All lists installed skills with on/off switches; the header shows how many are
// on and a search box that filters by name, description and category.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Filter, Package, Plus, Search, Sparkles, Wrench, X } from "lucide-react";
import { type ComponentType, useMemo, useState } from "react";
import { api } from "../lib/api.ts";
import type { SkillInfo } from "../lib/restTypes.ts";
import { HeaderActions, HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, CardHeader, inputClass, QueryState } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { Switch } from "../ui/Switch.tsx";
import { HubView } from "./skills/HubView.tsx";
import { LearnSkillDialog, SkillEditorDialog } from "./skills/SkillDialogs.tsx";
import { ToolsetsView } from "./skills/Toolsets.tsx";

type View = "installed" | "toolsets" | "hub";

const INSTALLED_KEY = ["skills", "installed"] as const;

/** "software-development" → "Software Development"; empty → "General". */
export function categoryLabel(category: string): string {
  const words = category.split(/[-_/\s]+/).filter(Boolean);
  if (words.length === 0) return "General";
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

function matchesSkill(skill: SkillInfo, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [skill.name, skill.description, skill.category].some((t) => t.toLowerCase().includes(q));
}

function PanelItem({
  icon: Icon,
  active,
  onClick,
  children,
}: {
  icon: ComponentType<{ className?: string }>;
  active: boolean;
  onClick: () => void;
  children: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cx(
        "type-label flex h-[1.6875rem] w-full items-center gap-2 px-2 text-left text-[0.6875rem] tracking-[0.08em] transition-colors",
        active ? "bg-accent text-on-accent" : "text-ink-muted hover:bg-hover",
      )}
    >
      <Icon className="size-3.5 shrink-0" />
      {children}
    </button>
  );
}

function SkillRow({ skill, onToggle }: { skill: SkillInfo; onToggle: (on: boolean) => void }) {
  return (
    <li className="flex items-start gap-3 px-4 py-2 hover:bg-hover">
      <span className="pt-0.5">
        <Switch checked={skill.enabled} onCheckedChange={onToggle} label={`Enable ${skill.name}`} />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span
          className={cx("type-data text-[0.875rem]", skill.enabled ? "text-ink" : "text-ink-soft")}
        >
          {skill.name}
        </span>
        {skill.description ? (
          <span className="line-clamp-2 text-[0.75rem] text-ink-muted">{skill.description}</span>
        ) : null}
      </div>
    </li>
  );
}

export function SkillsPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [view, setView] = useState<View>("installed");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<string | null>(null);
  const [editor, setEditor] = useState(false);
  const [learn, setLearn] = useState(false);

  const skills = useQuery({ queryKey: INSTALLED_KEY, queryFn: () => api.skills.list() });
  const toolsets = useQuery({
    queryKey: ["skills", "toolsets"],
    queryFn: () => api.skills.toolsets(),
  });
  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      api.skills.toggle(name, enabled),
    onSuccess: (_reply, { name, enabled }) => {
      client.setQueryData<{ skills: SkillInfo[] }>(INSTALLED_KEY, (data) =>
        data ? { skills: data.skills.map((s) => (s.name === name ? { ...s, enabled } : s)) } : data,
      );
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const all = skills.data?.skills ?? [];
  const enabled = all.filter((s) => s.enabled).length;
  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const skill of all) {
      const label = categoryLabel(skill.category);
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [all]);

  const searching = search.trim().length > 0;
  const shown = all.filter(
    (skill) =>
      matchesSkill(skill, search) &&
      (searching || category === null || categoryLabel(skill.category) === category),
  );
  const show = (next: View) => {
    setView(next);
    setSearch("");
  };

  return (
    <div className="flex flex-col gap-4 px-4 py-[1.4375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderAfterTitle>
        {skills.data ? (
          <span className="text-[0.75rem] text-ink-muted">
            {enabled}/{all.length} enabled
          </span>
        ) : null}
      </HeaderAfterTitle>
      <HeaderActions>
        <div className="relative w-[min(18.75rem,50vw)]">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute top-1/2 left-2.5 size-3 -translate-y-1/2 text-ink-faint"
          />
          <input
            type="search"
            aria-label="Search skills"
            placeholder="Search..."
            className={cx(inputClass, "h-[1.875rem] min-h-0 w-full pr-7 pl-7 text-[0.75rem]")}
            value={search}
            onChange={(e) => {
              setSearch(e.currentTarget.value);
              if (view === "hub") setView("installed");
            }}
          />
          {search ? (
            <button
              type="button"
              aria-label="Clear search"
              onClick={() => setSearch("")}
              className="absolute top-1/2 right-2 -translate-y-1/2 text-ink-soft hover:text-ink"
            >
              <X className="size-3" />
            </button>
          ) : null}
        </div>
      </HeaderActions>

      <div className="flex flex-col items-start gap-4 md:flex-row">
        <nav
          aria-label="Skill filters"
          className="w-full shrink-0 border border-line bg-surface md:sticky md:top-4 md:w-[13.125rem]"
        >
          <div className="type-label flex h-[1.9375rem] items-center gap-2 border-b border-line px-3 text-[0.6875rem] text-ink-soft">
            <Filter aria-hidden="true" className="size-3" />
            Filters
          </div>
          <div className="flex flex-col gap-1 p-2">
            <PanelItem
              icon={Package}
              active={view === "installed" && !searching}
              onClick={() => {
                show("installed");
                setCategory(null);
              }}
            >
              {`All (${all.length})`}
            </PanelItem>
            <PanelItem
              icon={Wrench}
              active={view === "toolsets" && !searching}
              onClick={() => show("toolsets")}
            >
              {`Toolsets (${toolsets.data?.toolsets.length ?? 0})`}
            </PanelItem>
            <PanelItem icon={Search} active={view === "hub"} onClick={() => show("hub")}>
              Browse hub
            </PanelItem>
          </div>
          {view === "installed" && !searching && categories.length ? (
            <div className="border-t border-line p-2">
              <p className="type-label m-0 px-1 pt-1 pb-2 text-[0.6875rem] text-ink-soft">
                Categories
              </p>
              <ul className="m-0 flex max-h-[60vh] list-none flex-col overflow-y-auto p-0">
                {categories.map(([label, count]) => (
                  <li key={label}>
                    <button
                      type="button"
                      aria-pressed={category === label}
                      onClick={() => setCategory(category === label ? null : label)}
                      className={cx(
                        "flex h-[1.4375rem] w-full items-center justify-between gap-2 px-1.5 text-left font-mono text-[0.6875rem]",
                        category === label
                          ? "bg-accent text-on-accent"
                          : "text-ink-muted hover:bg-hover",
                      )}
                    >
                      <span className="truncate">{label}</span>
                      <span
                        className={cx(
                          "tabular-nums",
                          category === label ? "text-on-accent" : "text-ink-soft",
                        )}
                      >
                        {count}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </nav>

        <div className="w-full min-w-0 flex-1">
          {view === "toolsets" ? (
            <ToolsetsView query={search} />
          ) : view === "hub" ? (
            <HubView />
          ) : (
            <Card padded={false}>
              <CardHeader
                icon={searching ? Search : Package}
                title={searching ? "Skills" : (category ?? "All")}
                aside={
                  <>
                    <Badge tone="secondary">
                      {shown.length} {searching ? "results" : "skills"}
                    </Badge>
                    <Button size="sm" icon={<Sparkles />} onClick={() => setLearn(true)}>
                      Learn a skill
                    </Button>
                    <Button size="sm" icon={<Plus />} onClick={() => setEditor(true)}>
                      New skill
                    </Button>
                  </>
                }
              />
              <QueryState loading={skills.isPending} error={skills.error}>
                {all.length === 0 ? (
                  <div className="flex flex-col items-center gap-3 px-4 py-12 text-center">
                    <p className="m-0 text-[0.8125rem] text-ink-muted">No skills installed yet.</p>
                    <div className="flex gap-2">
                      <Button tone="primary" size="sm" onClick={() => show("hub")}>
                        Browse hub
                      </Button>
                      <Button size="sm" icon={<Plus />} onClick={() => setEditor(true)}>
                        New skill
                      </Button>
                    </div>
                  </div>
                ) : shown.length === 0 ? (
                  <p className="m-0 px-4 py-12 text-center text-[0.8125rem] text-ink-muted">
                    No skills match
                  </p>
                ) : (
                  <ul
                    className="m-0 flex list-none flex-col px-0 py-2"
                    aria-label="Installed skills"
                  >
                    {shown.map((skill) => (
                      <SkillRow
                        key={skill.name}
                        skill={skill}
                        onToggle={(on) => toggle.mutate({ name: skill.name, enabled: on })}
                      />
                    ))}
                  </ul>
                )}
              </QueryState>
            </Card>
          )}
        </div>
      </div>
      <SkillEditorDialog open={editor} onClose={() => setEditor(false)} />
      <LearnSkillDialog open={learn} onClose={() => setLearn(false)} />
    </div>
  );
}
