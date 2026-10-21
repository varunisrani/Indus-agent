// The profile builder (P18-24): a stepped create flow — name, description, model, skills, MCP
// servers, review. Nothing is written until "Create profile" on the review step; then the profile
// is made (`POST /api/profiles`) and the chosen model, switched-off skills and new MCP servers are
// written into it, each with `?profile=<new name>`. A refused follow-up is listed; the profile stays.

import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useId, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api.ts";
import type { McpServerCreate, ProfileCreate } from "../lib/restTypes.ts";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText } from "../shell/Toasts.tsx";
import { Button, Card, Field, inputClass, Notice } from "../ui/controls.tsx";
import { parseEnvLines, splitArgs } from "./McpPage.tsx";
import { normaliseProfileName, profileNameProblem } from "./profiles/profileName.ts";

export const STEPS = [
  { id: "name", label: "Name" },
  { id: "description", label: "Description" },
  { id: "model", label: "Model" },
  { id: "skills", label: "Skills" },
  { id: "mcp", label: "MCP servers" },
  { id: "review", label: "Review" },
] as const;
type StepId = (typeof STEPS)[number]["id"];

interface McpDraft {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  env: string;
  url: string;
}

const EMPTY_MCP: McpDraft = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  env: "",
  url: "",
};

/** The add body for a drafted server, or why it cannot be added. */
export function mcpBody(draft: McpDraft): McpServerCreate | string {
  const name = draft.name.trim();
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(name)) return "Name the server with letters and digits.";
  if (draft.transport === "http") {
    if (!/^https?:\/\/\S+$/.test(draft.url.trim())) return "Give the server's http(s) URL.";
    return { name, url: draft.url.trim() };
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

function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={`${title} step`} className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export function ProfileBuilderPage() {
  const scope = useProfileScope();
  const ids = useId();
  const [step, setStep] = useState<StepId>("name");
  const [name, setName] = useState("");
  const [cloneFrom, setCloneFrom] = useState("");
  const [description, setDescription] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [keepSkills, setKeepSkills] = useState(true);
  const [skillsOff, setSkillsOff] = useState<string[]>([]);
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
    enabled: step === "skills",
  });

  const at = STEPS.findIndex((s) => s.id === step);
  const modelProblem = provider && !model.trim() ? "Name the model for that provider." : null;
  const blocked = (step === "name" && nameProblem !== null) || (step === "model" && !!modelProblem);
  const go = (delta: number) => {
    const next = STEPS[Math.min(Math.max(at + delta, 0), STEPS.length - 1)];
    if (next) setStep(next.id);
  };

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
    setDraft(EMPTY_MCP);
    setDraftProblem("");
  };

  const create = async () => {
    const id = normaliseProfileName(name);
    setCreating(true);
    const done: Outcome[] = [];
    const body: ProfileCreate = { name: id };
    if (cloneFrom) body.clone_from = cloneFrom;
    if (description.trim()) body.description = description.trim();
    if (!keepSkills) body.no_skills = true;
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
    if (keepSkills) {
      for (const skill of skillsOff) {
        await follow(`Skill ${skill} off`, () => api.skills.toggle(skill, false, id));
      }
    }
    for (const server of servers) {
      await follow(`MCP server ${server.name}`, () => api.mcp.add(server, id));
    }
    scope.refresh();
    setOutcomes(done);
    setCreating(false);
  };

  if (outcomes) {
    const made = outcomes[0]?.ok === true;
    return (
      <div className="flex flex-col gap-4 p-4">
        <Card className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">
            {made ? "Profile created" : "The profile was not created"}
          </h2>
          <ul aria-label="Create results" className="flex flex-col gap-1 text-sm">
            {outcomes.map((o) => (
              <li key={o.label} className={o.ok ? "text-fg" : "text-danger"}>
                {o.ok ? "✓" : "✗"} {o.label}
                {o.ok ? "" : `: ${o.message}`}
              </li>
            ))}
          </ul>
          <div className="flex gap-2">
            <Link
              to={{ pathname: "/profiles", search: scope.search }}
              className="rounded-md border border-line px-3 py-1.5 text-sm"
            >
              Back to profiles
            </Link>
            {made ? null : <Button onClick={() => setOutcomes(null)}>Change and retry</Button>}
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <ol aria-label="Steps" className="flex flex-wrap gap-1">
        {STEPS.map((s, i) => (
          <li key={s.id}>
            <button
              type="button"
              aria-current={s.id === step ? "step" : undefined}
              disabled={i > at && (nameProblem !== null || !!modelProblem)}
              onClick={() => setStep(s.id)}
              className={`rounded-md border px-2 py-1 text-xs ${
                s.id === step ? "border-accent bg-accent text-accent-fg" : "border-line"
              }`}
            >
              {i + 1}. {s.label}
            </button>
          </li>
        ))}
      </ol>
      <Card className="flex flex-col gap-3">
        {step === "name" ? (
          <Step title="Name">
            <Field
              label="Profile name"
              htmlFor={`${ids}-name`}
              hint="Lower-case letters, digits, '-' or '_'; at most 64 characters."
            >
              <input
                id={`${ids}-name`}
                className={inputClass}
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
              />
            </Field>
            {name && nameProblem ? <p className="text-xs text-danger">{nameProblem}</p> : null}
            <Field label="Start from" htmlFor={`${ids}-clone`}>
              <select
                id={`${ids}-clone`}
                className={inputClass}
                value={cloneFrom}
                onChange={(e) => setCloneFrom(e.currentTarget.value)}
              >
                <option value="">A fresh profile</option>
                {existing.map((p) => (
                  <option key={p} value={p}>
                    A copy of {p}'s settings
                  </option>
                ))}
              </select>
            </Field>
          </Step>
        ) : null}
        {step === "description" ? (
          <Step title="Description">
            <Field
              label="Description"
              htmlFor={`${ids}-description`}
              hint="What this profile is for; shown on the Profiles page."
            >
              <textarea
                id={`${ids}-description`}
                className={inputClass}
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.currentTarget.value)}
              />
            </Field>
          </Step>
        ) : null}
        {step === "model" ? (
          <Step title="Model">
            <Field label="Provider" htmlFor={`${ids}-provider`}>
              <select
                id={`${ids}-provider`}
                className={inputClass}
                value={provider}
                onChange={(e) => setProvider(e.currentTarget.value)}
              >
                <option value="">Keep the model it starts with</option>
                {(options.data?.providers ?? []).map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.label}
                    {p.configured ? "" : " (no key yet)"}
                  </option>
                ))}
              </select>
            </Field>
            {provider ? (
              <Field label="Model" htmlFor={`${ids}-model`}>
                <input
                  id={`${ids}-model`}
                  className={inputClass}
                  value={model}
                  onChange={(e) => setModel(e.currentTarget.value)}
                />
              </Field>
            ) : null}
            {modelProblem ? <p className="text-xs text-danger">{modelProblem}</p> : null}
          </Step>
        ) : null}
        {step === "skills" ? (
          <Step title="Skills">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={`${ids}-skills`}
                checked={keepSkills}
                onChange={() => setKeepSkills(true)}
              />
              Keep the bundled skills
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={`${ids}-skills`}
                checked={!keepSkills}
                onChange={() => setKeepSkills(false)}
              />
              Start without bundled skills
            </label>
            {keepSkills ? (
              <fieldset className="flex flex-col gap-1 border-0 p-0">
                <legend className="mb-1 text-xs text-muted">
                  Untick a skill to switch it off in the new profile.
                </legend>
                {skills.isPending ? <p className="text-sm text-muted">Loading…</p> : null}
                {(skills.data?.skills ?? []).map((skill) => (
                  <label key={skill.name} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={!skillsOff.includes(skill.name)}
                      onChange={(e) => {
                        const on = e.currentTarget.checked;
                        setSkillsOff((all) =>
                          on ? all.filter((n) => n !== skill.name) : [...all, skill.name],
                        );
                      }}
                    />
                    {skill.name}
                  </label>
                ))}
              </fieldset>
            ) : null}
          </Step>
        ) : null}
        {step === "mcp" ? (
          <Step title="MCP servers">
            {servers.length ? (
              <ul aria-label="Servers to add" className="flex flex-col gap-1 text-sm">
                {servers.map((s) => (
                  <li key={s.name} className="flex items-center gap-2">
                    <code className="flex-1 font-mono text-xs">
                      {s.name}: {s.url ?? [s.command, ...(s.args ?? [])].join(" ")}
                    </code>
                    <Button
                      tone="ghost"
                      aria-label={`Drop ${s.name}`}
                      onClick={() => setServers((all) => all.filter((x) => x.name !== s.name))}
                    >
                      Drop
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted">No MCP servers; this step is optional.</p>
            )}
            <Field label="Server name" htmlFor={`${ids}-mcp-name`}>
              <input
                id={`${ids}-mcp-name`}
                className={inputClass}
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })}
              />
            </Field>
            <Field label="Transport" htmlFor={`${ids}-mcp-transport`}>
              <select
                id={`${ids}-mcp-transport`}
                className={inputClass}
                value={draft.transport}
                onChange={(e) =>
                  setDraft({
                    ...EMPTY_MCP,
                    name: draft.name,
                    transport: e.currentTarget.value as McpDraft["transport"],
                  })
                }
              >
                <option value="stdio">stdio (a local command)</option>
                <option value="http">HTTP (a URL)</option>
              </select>
            </Field>
            {draft.transport === "stdio" ? (
              <>
                <Field label="Command" htmlFor={`${ids}-mcp-command`}>
                  <input
                    id={`${ids}-mcp-command`}
                    className={`${inputClass} font-mono`}
                    value={draft.command}
                    onChange={(e) => setDraft({ ...draft, command: e.currentTarget.value })}
                  />
                </Field>
                <Field label="Arguments" htmlFor={`${ids}-mcp-args`}>
                  <input
                    id={`${ids}-mcp-args`}
                    className={`${inputClass} font-mono`}
                    value={draft.args}
                    onChange={(e) => setDraft({ ...draft, args: e.currentTarget.value })}
                  />
                </Field>
                <Field label="Environment (KEY=value per line)" htmlFor={`${ids}-mcp-env`}>
                  <textarea
                    id={`${ids}-mcp-env`}
                    className={`${inputClass} font-mono`}
                    rows={2}
                    value={draft.env}
                    onChange={(e) => setDraft({ ...draft, env: e.currentTarget.value })}
                  />
                </Field>
              </>
            ) : (
              <Field label="URL" htmlFor={`${ids}-mcp-url`}>
                <input
                  id={`${ids}-mcp-url`}
                  className={inputClass}
                  value={draft.url}
                  onChange={(e) => setDraft({ ...draft, url: e.currentTarget.value })}
                />
              </Field>
            )}
            {draftProblem ? <Notice tone="danger">{draftProblem}</Notice> : null}
            <div>
              <Button onClick={addServer}>Add to the list</Button>
            </div>
          </Step>
        ) : null}
        {step === "review" ? (
          <Step title="Review">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-muted">Name</dt>
              <dd>{normaliseProfileName(name)}</dd>
              <dt className="text-muted">Starts from</dt>
              <dd>{cloneFrom ? `a copy of ${cloneFrom}` : "a fresh profile"}</dd>
              <dt className="text-muted">Description</dt>
              <dd>{description.trim() || "none"}</dd>
              <dt className="text-muted">Model</dt>
              <dd>{provider ? `${model.trim()} (${provider})` : "unchanged"}</dd>
              <dt className="text-muted">Skills</dt>
              <dd>
                {keepSkills
                  ? skillsOff.length
                    ? `bundled, without ${skillsOff.join(", ")}`
                    : "bundled"
                  : "none bundled"}
              </dd>
              <dt className="text-muted">MCP servers</dt>
              <dd>{servers.length ? servers.map((s) => s.name).join(", ") : "none"}</dd>
            </dl>
          </Step>
        ) : null}
      </Card>
      <div className="flex gap-2">
        <Button onClick={() => go(-1)} disabled={at === 0 || creating}>
          Back
        </Button>
        {step === "review" ? (
          <Button
            tone="primary"
            onClick={() => void create()}
            disabled={creating || nameProblem !== null}
          >
            {creating ? "Creating…" : "Create profile"}
          </Button>
        ) : (
          <Button tone="primary" onClick={() => go(1)} disabled={blocked}>
            Next
          </Button>
        )}
      </div>
    </div>
  );
}
