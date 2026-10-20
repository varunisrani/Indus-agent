// Skills (P18-22): installed skills with on/off switches, the toolsets view, and the skills hub with
// the scanner's verdict on every result (a `dangerous` skill cannot be installed).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../lib/api.ts";
import type { HubResult, ScanVerdict, SkillInfo } from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  type BadgeTone,
  Button,
  Card,
  inputClass,
  Notice,
  QueryState,
  Segmented,
} from "../ui/controls.tsx";
import { Switch } from "../ui/Switch.tsx";

type View = "installed" | "toolsets" | "hub";

const VERDICT_TONE: Record<ScanVerdict, BadgeTone> = {
  safe: "ok",
  caution: "warn",
  dangerous: "danger",
};

function InstalledView() {
  const toast = useToast();
  const client = useQueryClient();
  const skills = useQuery({ queryKey: ["skills", "installed"], queryFn: () => api.skills.list() });
  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      api.skills.toggle(name, enabled),
    onSuccess: (_reply, { name, enabled }) => {
      client.setQueryData<{ skills: SkillInfo[] }>(["skills", "installed"], (data) =>
        data ? { skills: data.skills.map((s) => (s.name === name ? { ...s, enabled } : s)) } : data,
      );
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const list = skills.data?.skills ?? [];
  return (
    <QueryState
      loading={skills.isPending}
      error={skills.error}
      empty={list.length === 0}
      emptyText="No skills installed."
    >
      <ul className="flex flex-col gap-2" aria-label="Installed skills">
        {list.map((skill) => (
          <li
            key={skill.name}
            className="flex items-center gap-3 rounded-md border border-line bg-raised p-3"
          >
            <div className="flex flex-1 flex-col">
              <span className="text-sm font-medium">{skill.name}</span>
              <span className="text-xs text-muted">{skill.description}</span>
            </div>
            <Badge>{skill.category}</Badge>
            <Switch
              checked={skill.enabled}
              onCheckedChange={(enabled) => toggle.mutate({ name: skill.name, enabled })}
              label={`Enable ${skill.name}`}
            />
          </li>
        ))}
      </ul>
    </QueryState>
  );
}

function ToolsetsView() {
  const toolsets = useQuery({
    queryKey: ["skills", "toolsets"],
    queryFn: () => api.skills.toolsets(),
  });
  const list = toolsets.data?.toolsets ?? [];
  return (
    <QueryState
      loading={toolsets.isPending}
      error={toolsets.error}
      empty={list.length === 0}
      emptyText="No toolsets reported."
    >
      <ul className="flex flex-col gap-2" aria-label="Toolsets">
        {list.map((ts) => (
          <li
            key={ts.name}
            className="flex flex-col gap-1 rounded-md border border-line bg-raised p-3"
          >
            <div className="flex items-center gap-2">
              <span className="flex-1 text-sm font-medium">{ts.label || ts.name}</span>
              <Badge tone={ts.enabled ? "ok" : "neutral"}>{ts.enabled ? "on" : "off"}</Badge>
            </div>
            <span className="text-xs text-muted">{ts.description}</span>
            {ts.tools.length ? (
              <span className="font-mono text-xs">{ts.tools.join(", ")}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </QueryState>
  );
}

function HubRow({ hit }: { hit: HubResult }) {
  const toast = useToast();
  const client = useQueryClient();
  const [problem, setProblem] = useState("");
  const done = (message: string) => {
    setProblem("");
    toast.show(message, "success");
    void client.invalidateQueries({ queryKey: ["skills"] });
  };
  const fail = (error: unknown) => {
    if (error instanceof ApiError && error.status === 409) {
      setProblem(`${error.message} The install was refused.`);
    } else {
      setProblem(errorText(error));
    }
  };
  const install = useMutation({
    mutationFn: () => api.skills.hubInstall(hit.identifier),
    onSuccess: () => done(`${hit.name} installed.`),
    onError: fail,
  });
  const update = useMutation({
    mutationFn: () => api.skills.hubUpdate(hit.name),
    onSuccess: () => done(`${hit.name} updated.`),
    onError: fail,
  });
  const uninstall = useMutation({
    mutationFn: () => api.skills.hubUninstall(hit.name),
    onSuccess: () => done(`${hit.name} removed.`),
    onError: fail,
  });
  const blocked = hit.verdict === "dangerous";
  return (
    <li
      className="flex flex-col gap-2 rounded-md border border-line bg-raised p-3"
      data-testid={`hub-${hit.identifier}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 text-sm font-medium">{hit.name}</span>
        {hit.verdict === null ? (
          <Badge>scanned on install</Badge>
        ) : (
          <Badge tone={VERDICT_TONE[hit.verdict]}>scan: {hit.verdict}</Badge>
        )}
        <Badge>{hit.source}</Badge>
      </div>
      <p className="text-xs text-muted">{hit.description}</p>
      <div className="flex flex-wrap gap-1">
        {hit.installed ? (
          <>
            {hit.update_available ? (
              <Button onClick={() => update.mutate()} disabled={update.isPending}>
                Update
              </Button>
            ) : null}
            <Button tone="danger" onClick={() => uninstall.mutate()} disabled={uninstall.isPending}>
              Uninstall
            </Button>
          </>
        ) : (
          <Button
            tone="primary"
            onClick={() => install.mutate()}
            disabled={blocked || install.isPending}
          >
            {blocked ? "Blocked by the scanner" : "Install"}
          </Button>
        )}
      </div>
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
    </li>
  );
}

function HubView() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const results = useQuery({
    queryKey: ["skills", "hub", query],
    queryFn: () => api.skills.hubSearch(query),
    enabled: query.length > 0,
  });
  const list = results.data?.results ?? [];
  return (
    <div className="flex flex-col gap-3">
      <search>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setQuery(draft.trim());
          }}
        >
          <input
            aria-label="Search the skills hub"
            className={`${inputClass} flex-1`}
            value={draft}
            onChange={(e) => setDraft(e.currentTarget.value)}
          />
          <Button type="submit">Search</Button>
        </form>
      </search>
      {query ? (
        <QueryState
          loading={results.isPending}
          error={results.error}
          empty={list.length === 0}
          emptyText="Nothing in the hub matches."
        >
          <ul className="flex flex-col gap-2" aria-label="Hub results">
            {list.map((hit) => (
              <HubRow key={hit.identifier} hit={hit} />
            ))}
          </ul>
        </QueryState>
      ) : (
        <p className="text-sm text-muted">
          Search the hub for skills to install. Every result shows the safety scanner's verdict.
        </p>
      )}
    </div>
  );
}

export function SkillsPage() {
  const [view, setView] = useState<View>("installed");
  return (
    <div className="flex flex-col gap-4 p-4">
      <Segmented
        label="View"
        value={view}
        options={[
          { value: "installed", label: "Installed" },
          { value: "toolsets", label: "Toolsets" },
          { value: "hub", label: "Hub" },
        ]}
        onChange={setView}
      />
      <Card>
        {view === "installed" ? (
          <InstalledView />
        ) : view === "toolsets" ? (
          <ToolsetsView />
        ) : (
          <HubView />
        )}
      </Card>
    </div>
  );
}
