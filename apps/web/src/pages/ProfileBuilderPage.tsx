// The profile builder (P18-24, five steps since W52-e): identity (name, description, start from),
// model, skills (bundled set and hub picks), MCP servers, review. Nothing is written until "Create
// profile" on the review step; then the profile is made (`POST /api/profiles`) and the chosen model,
// switched-off skills, hub skills and new MCP servers are written into it, each with
// `?profile=<new name>`. A refused follow-up is listed; the profile stays.

import { useMutation, useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { type ReactNode, useId, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api.ts";
import type { HubResult, McpAuth, McpServerCreate, ProfileCreate } from "../lib/restTypes.ts";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText } from "../shell/Toasts.tsx";
import { Badge, Button, Checkbox, inputClass, Notice, Segmented, Select } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { parseEnvLines, splitArgs } from "./McpPage.tsx";
import { normaliseProfileName, profileNameProblem } from "./profiles/profileName.ts";

/** The builder's steps in order; the pill shows "<n>. <title>". */
export const STEP_ORDER = ["identity", "model", "skills", "mcp", "review"] as const;
type StepId = (typeof STEP_ORDER)[number];
const STEP_TITLE: Record<StepId, string> = {
  review: "Review",
  mcp: "MCPs",
  skills: "Skills",
  model: "Model",
  identity: "Identity",
};
export const STEPS = STEP_ORDER.map((id) => ({ id, title: STEP_TITLE[id] }));

interface McpDraft {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  env: string;
  url: string;
  auth?: McpAuth;
}

const EMPTY_MCP: McpDraft = {
  name: "",
  transport: "http",
  command: "",
  args: "",
  env: "",
  url: "",
  auth: "none",
};

/** The add body for a drafted server, or why it cannot be added. */
export function mcpBody(draft: McpDraft): McpServerCreate | string {
  const name = draft.name.trim();
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(name)) return "Name the server with letters and digits.";
  if (draft.transport === "http") {
    if (!/^https?:\/\/\S+$/.test(draft.url.trim())) return "Give the server's http(s) URL.";
    const body: McpServerCreate = { name, url: draft.url.trim() };
    if (draft.auth && draft.auth !== "none") body.auth = draft.auth;
    return body;
  }
  if (!draft.command.trim()) return "Give the command that starts the server.";
  const env = parseEnvLines(draft.env);
  if (env === null) return "Write environment variables as KEY=value, one per line.";
  return { name, command: draft.command.trim(), args: splitArgs(draft.args), env };
}

interface Outcome {
  label: string;
  ok: boolean;
  message: string;
}

const labelClass = "type-label text-[0.6875rem] tracking-[0.1em] text-ink";
const fieldClass = cx(inputClass, "h-[33px] w-full text-[0.8125rem]");
const mutedLine = "m-0 text-[0.8125rem] text-ink-soft";
/** Back / Next / Create: tall, bold, tracked, not uppercase; disabled shows the muted fill. */
const navClass =
  "inline-flex h-[37px] min-w-[73px] items-center justify-center px-4 text-[0.875rem] font-bold tracking-[0.15em] transition-colors disabled:cursor-not-allowed disabled:bg-selected disabled:text-ink";

function Label({ htmlFor, children }: { htmlFor: string; children: ReactNode }) {
  return (
    <label htmlFor={htmlFor} className={labelClass}>
      {children}
    </label>
  );
}

function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={`${title} step`} className="flex flex-col gap-4">
      {children}
    </section>
  );
}

function Pill({
  index,
  label,
  state,
  disabled,
  onClick,
}: {
  index: number;
  label: string;
  state: "current" | "visited" | "future";
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-current={state === "current" ? "step" : undefined}
      disabled={disabled}
      onClick={onClick}
      className={cx(
        "h-[26px] rounded-full px-3 text-[0.8125rem] whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        state === "current" && "bg-accent text-on-accent",
        state === "visited" && "bg-selected text-ink hover:bg-hover",
        state === "future" && "text-ink-faint hover:text-ink-muted",
      )}
    >
      {index + 1}. {label}
    </button>
  );
}

export function ProfileBuilderPage() {
  const scope = useProfileScope();
  const ids = useId();
  const [step, setStep] = useState<StepId>("identity");
  const [name, setName] = useState("");
  const [cloneFrom, setCloneFrom] = useState("");
  const [description, setDescription] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [keepAll, setKeepAll] = useState(true);
  const [noSkills, setNoSkills] = useState(false);
  const [skillFilter, setSkillFilter] = useState("");
  const [skillsOff, setSkillsOff] = useState<string[]>([]);
  const [hubQuery, setHubQuery] = useState("");
  const [hubPicks, setHubPicks] = useState<HubResult[]>([]);
  const [servers, setServers] = useState<McpServerCreate[]>([]);
  const [draft, setDraft] = useState<McpDraft>(EMPTY_MCP);
  const [draftProblem, setDraftProblem] = useState("");
  const [creating, setCreating] = useState(false);
  const [outcomes, setOutcomes] = useState<Outcome[] | null>(null);

  const existing = scope.profiles.map((p) => p.name);
  const nameProblem = profileNameProblem(name, existing);
  const options = useQuery({
    queryKey: ["builder", "model-options"],
    queryFn: () => api.model.options(),
    enabled: step === "model",
  });
  const skills = useQuery({
    queryKey: ["builder", "skills"],
    queryFn: () => api.skills.list(),
    enabled: step === "skills" && !keepAll,
  });
  const hub = useMutation({ mutationFn: (q: string) => api.skills.hubSearch(q) });

  const at = STEP_ORDER.indexOf(step);
  const modelProblem = provider && !model.trim() ? "Name the model for that provider." : null;
  const blocked =
    (step === "identity" && nameProblem !== null) || (step === "model" && !!modelProblem);
  const go = (delta: number) => {
    const next = STEP_ORDER[Math.min(Math.max(at + delta, 0), STEP_ORDER.length - 1)];
    if (next) setStep(next);
  };
  const trimSkills = !noSkills && !keepAll;

  const addServer = () => {
    const body = mcpBody(draft);
    if (typeof body === "string") {
      setDraftProblem(body);
      return;
    }
    if (servers.some((s) => s.name === body.name)) {
      setDraftProblem("A server with that name is already on the list.");
      return;
    }
    setServers((all) => [...all, body]);
    setDraft({ ...EMPTY_MCP, transport: draft.transport });
    setDraftProblem("");
  };

  const searchHub = () => {
    if (hubQuery.trim()) hub.mutate(hubQuery.trim());
  };

  const create = async () => {
    const id = normaliseProfileName(name);
    setCreating(true);
    const done: Outcome[] = [];
    const body: ProfileCreate = { name: id };
    if (cloneFrom) body.clone_from = cloneFrom;
    if (description.trim()) body.description = description.trim();
    if (noSkills) body.no_skills = true;
    try {
      const reply = await api.profile.create(body);
      done.push({ label: `Profile ${reply.name}`, ok: true, message: "created" });
    } catch (error) {
      setOutcomes([{ label: `Profile ${id}`, ok: false, message: errorText(error) }]);
      setCreating(false);
      return;
    }
    const follow = async (label: string, run: () => Promise<unknown>) => {
      try {
        await run();
        done.push({ label, ok: true, message: "done" });
      } catch (error) {
        done.push({ label, ok: false, message: errorText(error) });
      }
    };
    if (provider && model.trim()) {
      await follow(`Model ${model.trim()}`, () => api.profile.setModel(id, provider, model.trim()));
    }
    if (trimSkills) {
      for (const skill of skillsOff) {
        await follow(`Skill ${skill} off`, () => api.skills.toggle(skill, false, id));
      }
    }
    for (const pick of hubPicks) {
      await follow(`Hub skill ${pick.name}`, () => api.skills.hubInstall(pick.identifier, id));
    }
    for (const server of servers) {
      await follow(`MCP server ${server.name}`, () => api.mcp.add(server, id));
    }
    scope.refresh();
    setOutcomes(done);
    setCreating(false);
  };

  const shell = (children: ReactNode) => (
    <div className="px-4 py-[1.4375rem]">
      <div className="mx-auto flex w-full max-w-[690px] flex-col gap-6">{children}</div>
    </div>
  );

  if (outcomes) {
    const made = outcomes[0]?.ok === true;
    return shell(
      <section className="flex flex-col gap-4 border border-line p-5">
        <h2 className="type-section-title m-0 text-[0.9375rem]">
          {made ? "Profile created" : "The profile was not created"}
        </h2>
        <ul
          aria-label="Create results"
          className="m-0 flex list-none flex-col gap-1 p-0 text-[0.8125rem]"
        >
          {outcomes.map((o) => (
            <li key={o.label} className={o.ok ? "text-ink" : "text-danger"}>
              {o.ok ? "✓" : "✗"} {o.label}
              {o.ok ? "" : `: ${o.message}`}
            </li>
          ))}
        </ul>
        <div className="flex gap-2">
          <Link
            to={{ pathname: "/profiles", search: scope.search }}
            className="type-button inline-flex h-[1.875rem] items-center border border-accent bg-accent px-3 text-on-accent hover:opacity-90"
          >
            Back to profiles
          </Link>
          {made ? null : <Button onClick={() => setOutcomes(null)}>Change and retry</Button>}
        </div>
      </section>,
    );
  }

  const providers = (options.data?.providers ?? []).filter((p) => {
    const q = providerFilter.trim().toLowerCase();
    return !q || p.label.toLowerCase().includes(q) || p.name.toLowerCase().includes(q);
  });
  const skillRows = (skills.data?.skills ?? []).filter((s) => {
    const q = skillFilter.trim().toLowerCase();
    return !q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q);
  });
  const rowClass =
    "flex w-full items-center gap-2 px-3 py-2 text-left text-[0.8125rem] transition-colors";

  return shell(
    <>
      <div className="flex items-center gap-4">
        <h2 className="m-0 flex-1 font-display text-[2.5rem] leading-none font-bold text-ink">
          New profile
        </h2>
        <Link
          to={{ pathname: "/profiles", search: scope.search }}
          className="px-3 py-1.5 text-[0.8125rem] font-bold tracking-[0.15em] text-ink hover:bg-hover"
        >
          Cancel
        </Link>
      </div>

      <ol aria-label="Steps" className="m-0 flex list-none flex-wrap gap-2 p-0">
        {STEPS.map((s, i) => (
          <li key={s.id}>
            <Pill
              index={i}
              label={s.title}
              state={i === at ? "current" : i < at ? "visited" : "future"}
              disabled={(i > 0 && nameProblem !== null) || (i > at && !!modelProblem)}
              onClick={() => setStep(s.id)}
            />
          </li>
        ))}
      </ol>

      <div className="border border-line p-5">
        {step === "identity" ? (
          <Step title="Identity">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-name`}>Profile name</Label>
              <input
                id={`${ids}-name`}
                className={fieldClass}
                placeholder="coder"
                aria-invalid={name !== "" && nameProblem !== null}
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
              />
              {name && nameProblem ? (
                <p className="m-0 text-[0.75rem] text-danger">{nameProblem}</p>
              ) : null}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-description`}>Description (optional)</Label>
              <input
                id={`${ids}-description`}
                className={fieldClass}
                placeholder="What this profile is for"
                value={description}
                onChange={(e) => setDescription(e.currentTarget.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-clone`}>Start from</Label>
              <Select
                id={`${ids}-clone`}
                className="w-full [&_select]:h-[33px] [&_select]:text-[0.8125rem]"
                value={cloneFrom}
                onChange={(e) => setCloneFrom(e.currentTarget.value)}
              >
                <option value="">A fresh profile</option>
                {existing.map((p) => (
                  <option key={p} value={p}>
                    A copy of {p}'s settings
                  </option>
                ))}
              </Select>
            </div>
          </Step>
        ) : null}

        {step === "model" ? (
          <Step title="Model">
            <p className={mutedLine}>
              Pick the provider new sessions use; you can also leave it for later.
            </p>
            <input
              aria-label="Filter providers"
              className={fieldClass}
              placeholder="Filter…"
              value={providerFilter}
              onChange={(e) => setProviderFilter(e.currentTarget.value)}
            />
            <fieldset
              aria-label="Providers"
              className="m-0 flex max-h-72 min-w-0 flex-col overflow-y-auto border border-line p-0"
            >
              <button
                type="button"
                aria-pressed={provider === ""}
                onClick={() => {
                  setProvider("");
                  setModel("");
                }}
                className={cx(rowClass, provider === "" ? "bg-ink/10" : "hover:bg-hover")}
              >
                Use default (set later)
              </button>
              {options.isPending ? <p className={cx(mutedLine, "px-3 py-2")}>Loading…</p> : null}
              {providers.map((p) => (
                <button
                  key={p.name}
                  type="button"
                  aria-pressed={provider === p.name}
                  onClick={() => setProvider(p.name)}
                  className={cx(
                    rowClass,
                    "border-t border-line",
                    provider === p.name ? "bg-ink/10" : "hover:bg-hover",
                  )}
                >
                  <span className="flex-1">{p.label}</span>
                  <span className="type-data text-[0.75rem] text-ink-soft">
                    {p.configured ? p.name : "no key yet"}
                  </span>
                </button>
              ))}
            </fieldset>
            {provider ? (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${ids}-model`}>Model</Label>
                <input
                  id={`${ids}-model`}
                  className={fieldClass}
                  placeholder="model name"
                  value={model}
                  onChange={(e) => setModel(e.currentTarget.value)}
                />
              </div>
            ) : null}
            {modelProblem ? <p className="m-0 text-[0.75rem] text-danger">{modelProblem}</p> : null}
          </Step>
        ) : null}

        {step === "skills" ? (
          <Step title="Skills">
            <label htmlFor={`${ids}-keep`} className="flex items-center gap-2 text-[0.8125rem]">
              <Checkbox
                id={`${ids}-keep`}
                checked={keepAll && !noSkills}
                disabled={noSkills}
                onChange={(e) => setKeepAll(e.currentTarget.checked)}
              />
              Keep every bundled skill (recommended)
            </label>
            <label htmlFor={`${ids}-none`} className="flex items-center gap-2 text-[0.8125rem]">
              <Checkbox
                id={`${ids}-none`}
                checked={noSkills}
                onChange={(e) => setNoSkills(e.currentTarget.checked)}
              />
              Start with no bundled skills
            </label>
            {trimSkills ? (
              <fieldset className="m-0 flex flex-col gap-2 border-0 p-0">
                <legend className="mb-2 p-0 text-[0.75rem] text-ink-soft">
                  Untick a skill to switch it off in the new profile.
                </legend>
                <input
                  aria-label="Filter skills"
                  className={fieldClass}
                  placeholder="Filter…"
                  value={skillFilter}
                  onChange={(e) => setSkillFilter(e.currentTarget.value)}
                />
                <div className="flex max-h-56 flex-col overflow-y-auto border border-line">
                  {skills.isPending ? <p className={cx(mutedLine, "px-3 py-2")}>Loading…</p> : null}
                  {skillRows.map((skill, i) => (
                    <label
                      key={skill.name}
                      htmlFor={`${ids}-skill-${skill.name}`}
                      className={cx(
                        "flex cursor-pointer items-start gap-2.5 px-3 py-2 hover:bg-hover",
                        i > 0 && "border-t border-line",
                      )}
                    >
                      <Checkbox
                        id={`${ids}-skill-${skill.name}`}
                        aria-label={skill.name}
                        className="mt-0.5"
                        checked={!skillsOff.includes(skill.name)}
                        onChange={(e) => {
                          const on = e.currentTarget.checked;
                          setSkillsOff((all) =>
                            on ? all.filter((n) => n !== skill.name) : [...all, skill.name],
                          );
                        }}
                      />
                      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                        <span className="flex items-center gap-2">
                          <span className="text-[0.8125rem] font-medium text-ink">
                            {skill.name}
                          </span>
                          {skill.category ? <Badge tone="secondary">{skill.category}</Badge> : null}
                        </span>
                        {skill.description ? (
                          <span className="text-[0.75rem] text-ink-soft">{skill.description}</span>
                        ) : null}
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ) : null}
            <div className="flex flex-col gap-2 border-t border-line pt-4">
              <Label htmlFor={`${ids}-hub`}>Add from the skills hub</Label>
              <div className="flex gap-2">
                <input
                  id={`${ids}-hub`}
                  className={fieldClass}
                  placeholder="Search the hub"
                  value={hubQuery}
                  onChange={(e) => setHubQuery(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      searchHub();
                    }
                  }}
                />
                <Button className="h-[33px]" onClick={searchHub} disabled={hub.isPending}>
                  Search
                </Button>
              </div>
              {hub.error ? <Notice tone="danger">{errorText(hub.error)}</Notice> : null}
              {hubPicks.length ? (
                <ul
                  aria-label="Hub skills to add"
                  className="m-0 flex list-none flex-wrap gap-1.5 p-0"
                >
                  {hubPicks.map((pick) => (
                    <li key={pick.identifier}>
                      <Badge tone="accent">
                        {pick.name}
                        <button
                          type="button"
                          aria-label={`Remove ${pick.name}`}
                          className="-mr-1 inline-flex items-center"
                          onClick={() =>
                            setHubPicks((all) =>
                              all.filter((p) => p.identifier !== pick.identifier),
                            )
                          }
                        >
                          <X />
                        </button>
                      </Badge>
                    </li>
                  ))}
                </ul>
              ) : null}
              {hub.data ? (
                hub.data.results.length ? (
                  <ul
                    aria-label="Hub results"
                    className="m-0 flex max-h-56 list-none flex-col overflow-y-auto border border-line p-0"
                  >
                    {hub.data.results.map((hit, i) => {
                      const picked = hubPicks.some((p) => p.identifier === hit.identifier);
                      return (
                        <li
                          key={hit.identifier}
                          className={cx(
                            "flex items-start gap-2 px-3 py-2",
                            i > 0 && "border-t border-line",
                          )}
                        >
                          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                            <span className="flex items-center gap-2">
                              <span className="text-[0.8125rem] font-medium text-ink">
                                {hit.name}
                              </span>
                              <Badge tone="secondary">{hit.source}</Badge>
                            </span>
                            {hit.description ? (
                              <span className="text-[0.75rem] text-ink-soft">
                                {hit.description}
                              </span>
                            ) : null}
                          </span>
                          <Button
                            tone="ghost"
                            size="sm"
                            aria-label={`Add ${hit.name}`}
                            disabled={picked || hit.installed}
                            onClick={() => setHubPicks((all) => [...all, hit])}
                          >
                            {hit.installed ? "Installed" : picked ? "Added" : "Add"}
                          </Button>
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className={mutedLine}>No hub skills match that search.</p>
                )
              ) : null}
            </div>
          </Step>
        ) : null}

        {step === "mcp" ? (
          <Step title="MCP servers">
            <div className="flex items-baseline gap-3">
              <h3 className="m-0 flex-1 font-display text-[0.9375rem] font-bold text-ink [font-stretch:125%]">
                MCP servers
              </h3>
              <span className="text-[0.75rem] text-ink-soft">{servers.length} configured</span>
            </div>
            <p className={mutedLine}>
              Tool servers the new profile connects to. This step is optional.
            </p>
            <div className="flex flex-col gap-4 border border-line bg-surface p-4 sm:p-5">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`${ids}-mcp-name`}>Server name</Label>
                  <input
                    id={`${ids}-mcp-name`}
                    className={fieldClass}
                    placeholder="papers"
                    value={draft.name}
                    onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <span aria-hidden="true" className={labelClass}>
                    Transport
                  </span>
                  <Segmented
                    label="Transport"
                    value={draft.transport}
                    options={[
                      { value: "http", label: "HTTP/SSE" },
                      { value: "stdio", label: "stdio" },
                    ]}
                    onChange={(transport) =>
                      setDraft({ ...EMPTY_MCP, name: draft.name, transport })
                    }
                  />
                </div>
              </div>
              {draft.transport === "http" ? (
                <>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`${ids}-mcp-url`}>URL</Label>
                    <input
                      id={`${ids}-mcp-url`}
                      className={fieldClass}
                      placeholder="https://"
                      value={draft.url}
                      onChange={(e) => setDraft({ ...draft, url: e.currentTarget.value })}
                    />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <span aria-hidden="true" className={labelClass}>
                      Authentication
                    </span>
                    <Segmented
                      label="Authentication"
                      value={draft.auth ?? "none"}
                      options={[
                        { value: "none", label: "None" },
                        { value: "header", label: "Header" },
                        { value: "oauth", label: "OAuth" },
                      ]}
                      onChange={(auth) => setDraft({ ...draft, auth })}
                    />
                    {draft.auth === "header" ? (
                      <p className="m-0 text-[0.75rem] text-ink-soft">
                        The header's key is stored on the Keys page, not here.
                      </p>
                    ) : null}
                    {draft.auth === "oauth" ? (
                      <p className="m-0 text-[0.75rem] text-ink-soft">
                        Sign in from the MCP page once the profile exists.
                      </p>
                    ) : null}
                  </div>
                </>
              ) : (
                <>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor={`${ids}-mcp-command`}>Command</Label>
                      <input
                        id={`${ids}-mcp-command`}
                        className={fieldClass}
                        value={draft.command}
                        onChange={(e) => setDraft({ ...draft, command: e.currentTarget.value })}
                      />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor={`${ids}-mcp-args`}>Arguments</Label>
                      <input
                        id={`${ids}-mcp-args`}
                        className={fieldClass}
                        value={draft.args}
                        onChange={(e) => setDraft({ ...draft, args: e.currentTarget.value })}
                      />
                    </div>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`${ids}-mcp-env`}>Environment (KEY=value per line)</Label>
                    <textarea
                      id={`${ids}-mcp-env`}
                      className={cx(inputClass, "w-full py-2 text-[0.8125rem]")}
                      rows={2}
                      value={draft.env}
                      onChange={(e) => setDraft({ ...draft, env: e.currentTarget.value })}
                    />
                  </div>
                </>
              )}
              {draftProblem ? <Notice tone="danger">{draftProblem}</Notice> : null}
              <div className="flex justify-end">
                <Button tone="primary" onClick={addServer}>
                  Add server
                </Button>
              </div>
            </div>
            {servers.length ? (
              <ul aria-label="Servers to add" className="m-0 flex list-none flex-col gap-2 p-0">
                {servers.map((s) => (
                  <li
                    key={s.name}
                    className="flex items-start gap-3 border border-line bg-surface p-4"
                  >
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="text-[0.8125rem] font-medium text-ink">{s.name}</span>
                        <Badge tone="secondary">{s.url ? "http" : "stdio"}</Badge>
                        {s.url ? <Badge>{`auth: ${s.auth ?? "none"}`}</Badge> : null}
                      </span>
                      <code className="type-data text-[0.75rem] break-all text-ink-soft">
                        {s.url ?? [s.command, ...(s.args ?? [])].join(" ")}
                      </code>
                    </div>
                    <Button
                      tone="ghost"
                      size="sm"
                      className="text-danger hover:bg-danger-wash"
                      aria-label={`Remove ${s.name}`}
                      onClick={() => setServers((all) => all.filter((x) => x.name !== s.name))}
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            ) : null}
          </Step>
        ) : null}

        {step === "review" ? (
          <Step title="Review">
            <dl className="m-0 grid grid-cols-[96px_1fr] gap-x-4 gap-y-2 text-[0.8125rem]">
              <dt className="text-ink-soft">Name</dt>
              <dd className="m-0 text-ink">{normaliseProfileName(name)}</dd>
              <dt className="text-ink-soft">Description</dt>
              <dd className="m-0 text-ink">{description.trim() || "—"}</dd>
              <dt className="text-ink-soft">Starts from</dt>
              <dd className="m-0 text-ink">
                {cloneFrom ? `A copy of ${cloneFrom}` : "A fresh profile"}
              </dd>
              <dt className="text-ink-soft">Model</dt>
              <dd className="m-0 text-ink">
                {provider ? `${model.trim()} (${provider})` : "Default (set later)"}
              </dd>
              <dt className="text-ink-soft">Skills</dt>
              <dd className="m-0 text-ink">
                {noSkills
                  ? "No bundled skills"
                  : trimSkills && skillsOff.length
                    ? `Bundled, without ${skillsOff.join(", ")}`
                    : "Every bundled skill"}
              </dd>
              {hubPicks.length ? (
                <>
                  <dt className="text-ink-soft">Hub skills</dt>
                  <dd className="m-0 text-ink">{hubPicks.map((p) => p.name).join(", ")}</dd>
                </>
              ) : null}
              <dt className="text-ink-soft">MCP servers</dt>
              <dd className="m-0 text-ink">
                {servers.length ? servers.map((s) => s.name).join(", ") : "None"}
              </dd>
            </dl>
          </Step>
        ) : null}
      </div>

      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => go(-1)}
          disabled={at === 0 || creating}
          className={cx(navClass, "text-ink hover:bg-hover")}
        >
          Back
        </button>
        {step === "review" ? (
          <button
            type="button"
            onClick={() => void create()}
            disabled={creating || nameProblem !== null}
            className={cx(navClass, "bg-accent text-on-accent hover:opacity-90")}
          >
            {creating ? "Creating…" : "Create profile"}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => go(1)}
            disabled={blocked}
            className={cx(navClass, "bg-accent text-on-accent hover:opacity-90")}
          >
            Next
          </button>
        )}
      </div>
    </>,
  );
}
