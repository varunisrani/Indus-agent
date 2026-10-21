// The four tabs of the Capabilities page (P24-26). Each tab loads its own list for the profile it
// is given (the page remounts a tab when the profile changes) through the daemon's REST routes
// (`rest()`, main's proxy, P20-14) and writes through them too. Nothing is installed without a
// click: hub installs and install deep links go through a confirmation, plugin installs need a
// full 40-character commit pin.

import * as Dialog from "@radix-ui/react-dialog";
import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";

// -- shapes (the REST answers, P18-14) ------------------------------------------------------------

export interface SkillRow {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
  source: string;
}

export interface HubResult {
  identifier: string;
  name: string;
  description: string;
  source: string;
  installed: boolean;
}

export interface ToolsetRow {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  tools: string[];
}

export interface McpServerRow {
  name: string;
  enabled?: boolean;
  kind?: string;
  command?: string | null;
  url?: string | null;
  auth?: string | null;
}

export interface AgentPluginRow {
  name: string;
  version: string | null;
  description: string;
  enabled: boolean;
  sha: string | null;
}

export interface DesktopPluginRow {
  id: string;
  name: string;
  version?: string;
  enabled: boolean;
}

/** The app-level desktop plugins (U3's P24-32 contribution registry implements it). */
export interface DesktopPluginSource {
  list(): Promise<DesktopPluginRow[]>;
}

export type PluginBadge = "agent" | "desktop" | "both";

export interface PluginRowView {
  readonly name: string;
  readonly agent: AgentPluginRow | null;
  readonly desktop: DesktopPluginRow | null;
  readonly badge: PluginBadge;
}

/** One row per plugin name, with its Agent and Desktop halves. */
export function mergePlugins(
  agent: readonly AgentPluginRow[],
  desktop: readonly DesktopPluginRow[],
): PluginRowView[] {
  const names = new Map<
    string,
    { agent: AgentPluginRow | null; desktop: DesktopPluginRow | null }
  >();
  for (const row of agent) names.set(row.name, { agent: row, desktop: null });
  for (const row of desktop) {
    const key = row.name;
    const found = names.get(key);
    names.set(key, { agent: found?.agent ?? null, desktop: row });
  }
  return [...names.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, halves]) => ({
      name,
      ...halves,
      badge:
        halves.agent !== null && halves.desktop !== null
          ? "both"
          : halves.agent !== null
            ? "agent"
            : "desktop",
    }));
}

export const FULL_SHA = /^[0-9a-f]{40}$/i;

// -- small pieces ---------------------------------------------------------------------------------

const BUTTON =
  "rounded border border-line px-2 py-0.5 text-xs hover:bg-raised disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-focus";

/** A list loaded for one profile; `reload` re-reads it. */
function useList<T>(
  api: RestLike | null,
  path: string,
  profile: string,
  pick: (body: unknown) => T[],
): { rows: T[] | null; error: string | null; reload: () => void } {
  const [rows, setRows] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serial, setSerial] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: serial forces a reload
  useEffect(() => {
    let live = true;
    void rest<unknown>(api, "GET", path, { profile }).then((answer) => {
      if (!live) return;
      if (answer.ok) {
        setRows(pick(answer.data));
        setError(null);
      } else {
        setError(answer.message);
      }
    });
    return () => {
      live = false;
    };
  }, [api, path, profile, serial]);
  return { rows, error, reload: useCallback(() => setSerial((n) => n + 1), []) };
}

function field<T>(key: string): (body: unknown) => T[] {
  return (body) => {
    const value = (body as Record<string, unknown> | null)?.[key];
    return Array.isArray(value) ? (value as T[]) : [];
  };
}

function ListState(props: { error: string | null; loading: boolean; onRetry: () => void }) {
  const t = useT();
  if (props.error !== null) {
    return (
      <div role="alert" className="flex items-center gap-2 p-3 text-sm text-danger">
        {t("capabilities.failed", { message: props.error })}
        <button type="button" className={BUTTON} onClick={props.onRetry}>
          {t("capabilities.retry")}
        </button>
      </div>
    );
  }
  return props.loading ? <p className="p-3 text-sm text-muted">…</p> : null;
}

function Note(props: { answer: RestAnswer<unknown> | null; ok?: string }) {
  if (props.answer === null) return null;
  if (props.answer.ok) {
    return props.ok === undefined ? null : (
      <p role="status" className="px-3 py-1 text-xs text-ok">
        {props.ok}
      </p>
    );
  }
  return (
    <p role="alert" className="px-3 py-1 text-xs text-danger">
      {props.answer.message}
    </p>
  );
}

function Confirm(props: {
  title: string;
  body: ReactNode;
  confirm: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) props.onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="overlay-backdrop" />
        <Dialog.Content className="overlay-card">
          <Dialog.Title>{props.title}</Dialog.Title>
          <Dialog.Description>{props.body}</Dialog.Description>
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" className={BUTTON} onClick={props.onCancel}>
              {t("capabilities.cancel")}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={props.busy}
              onClick={props.onConfirm}
            >
              {props.confirm}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Toggle(props: { label: string; checked: boolean; onChange: (on: boolean) => void }) {
  return (
    <input
      type="checkbox"
      role="switch"
      aria-label={props.label}
      aria-checked={props.checked}
      checked={props.checked}
      onChange={(event) => props.onChange(event.target.checked)}
    />
  );
}

// -- skills ---------------------------------------------------------------------------------------

export function SkillsTab(props: {
  api: RestLike | null;
  profile: string;
  /** A hub identifier from an install deep link: confirmed before anything is called. */
  pendingInstall?: string | null;
  onPendingDone?: () => void;
}) {
  const t = useT();
  const { api, profile } = props;
  const list = useList<SkillRow>(api, "/api/skills", profile, field("skills"));
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<HubResult[]>([]);
  const [confirming, setConfirming] = useState<string | null>(props.pendingInstall ?? null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ answer: RestAnswer<unknown>; ok?: string } | null>(null);

  useEffect(() => {
    if (props.pendingInstall) setConfirming(props.pendingInstall);
  }, [props.pendingInstall]);

  const toggle = async (name: string, enabled: boolean) => {
    const answer = await rest(api, "POST", "/api/skills/toggle", {
      body: { name, enabled },
      profile,
    });
    setNote({ answer });
    list.reload();
  };

  const search = async (event: FormEvent) => {
    event.preventDefault();
    const answer = await rest<{ results?: HubResult[] }>(
      api,
      "GET",
      `/api/skills/hub/search?q=${encodeURIComponent(query.trim())}`,
      { profile },
    );
    if (answer.ok) setResults(answer.data.results ?? []);
    else setNote({ answer });
  };

  const install = async () => {
    if (confirming === null || busy) return;
    setBusy(true);
    const identifier = confirming;
    const answer = await rest<{ name?: string }>(api, "POST", "/api/skills/hub/install", {
      body: { identifier },
      profile,
      timeoutMs: 120_000,
    });
    setBusy(false);
    setConfirming(null);
    props.onPendingDone?.();
    setNote({
      answer,
      ...(answer.ok ? { ok: t("skills.done", { name: answer.data.name ?? identifier }) } : {}),
    });
    list.reload();
  };

  const uninstall = async (name: string) => {
    const answer = await rest(api, "POST", "/api/skills/hub/uninstall", {
      body: { name },
      profile,
    });
    setNote({ answer });
    list.reload();
  };

  return (
    <div className="flex flex-col" data-testid="tab-skills">
      <Note answer={note?.answer ?? null} {...(note?.ok === undefined ? {} : { ok: note.ok })} />
      <ListState error={list.error} loading={list.rows === null} onRetry={list.reload} />
      <ul className="divide-y divide-line">
        {(list.rows ?? []).map((skill) => (
          <li key={skill.name} className="flex items-center gap-3 px-3 py-2 text-sm">
            <Toggle
              label={t("skills.enabled", { name: skill.name })}
              checked={skill.enabled}
              onChange={(on) => void toggle(skill.name, on)}
            />
            <div className="min-w-0 flex-1">
              <p className="font-medium">{skill.name}</p>
              <p className="truncate text-xs text-muted">{skill.description}</p>
            </div>
            <span className="text-xs text-muted">
              {t("skills.source", { source: skill.source })}
            </span>
            {skill.source === "hub" ? (
              <button type="button" className={BUTTON} onClick={() => void uninstall(skill.name)}>
                {t("skills.uninstall")}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {list.rows !== null && list.rows.length === 0 ? (
        <p className="px-3 py-2 text-sm text-muted">{t("capabilities.empty")}</p>
      ) : null}
      <form className="flex gap-2 border-t border-line px-3 py-2" onSubmit={search}>
        <input
          className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-0.5 text-sm"
          aria-label={t("skills.search")}
          placeholder={t("skills.search")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button type="submit" className={BUTTON} disabled={query.trim() === ""}>
          {t("skills.searchButton")}
        </button>
      </form>
      <ul className="divide-y divide-line">
        {results.map((hit) => (
          <li key={hit.identifier} className="flex items-center gap-3 px-3 py-2 text-sm">
            <div className="min-w-0 flex-1">
              <p className="font-medium">{hit.name}</p>
              <p className="truncate text-xs text-muted">{hit.description}</p>
            </div>
            {hit.installed ? (
              <span className="text-xs text-muted">{t("skills.installed")}</span>
            ) : (
              <button
                type="button"
                className={BUTTON}
                onClick={() => setConfirming(hit.identifier)}
              >
                {t("skills.install")}
              </button>
            )}
          </li>
        ))}
      </ul>
      {confirming !== null ? (
        <Confirm
          title={t("skills.confirm.title")}
          body={t("skills.confirm.body", { identifier: confirming, profile })}
          confirm={t("skills.confirm.install")}
          busy={busy}
          onConfirm={() => void install()}
          onCancel={() => {
            setConfirming(null);
            props.onPendingDone?.();
          }}
        />
      ) : null}
    </div>
  );
}

// -- toolsets -------------------------------------------------------------------------------------

export function ToolsetsTab(props: { api: RestLike | null; profile: string }) {
  const t = useT();
  const list = useList<ToolsetRow>(
    props.api,
    "/api/tools/toolsets",
    props.profile,
    field("toolsets"),
  );
  return (
    <div className="flex flex-col" data-testid="tab-toolsets">
      <ListState error={list.error} loading={list.rows === null} onRetry={list.reload} />
      <ul className="divide-y divide-line">
        {(list.rows ?? []).map((set) => (
          <li key={set.name} className="px-3 py-2 text-sm">
            <details>
              <summary className="flex cursor-pointer items-center gap-2">
                <span className="font-medium">{set.label}</span>
                <span className="text-xs text-muted">
                  {t("toolsets.tools", { count: set.tools.length })}
                </span>
              </summary>
              <p className="text-xs text-muted">{set.description}</p>
              <ul className="mt-1 flex flex-wrap gap-1">
                {set.tools.map((tool) => (
                  <li key={tool} className="rounded bg-code px-1 font-mono text-xs">
                    {tool}
                  </li>
                ))}
              </ul>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

// -- connectors (MCP) -----------------------------------------------------------------------------

export function ConnectorsTab(props: {
  api: RestLike | null;
  profile: string;
  /** Open the sign-in page in the browser (`external.open`, a real click in this window). */
  openUrl?: (url: string) => void;
}) {
  const t = useT();
  const { api, profile } = props;
  const list = useList<McpServerRow>(api, "/api/mcp/servers", profile, field("servers"));
  const [draft, setDraft] = useState({ name: "", command: "", url: "", oauth: false });
  const [note, setNote] = useState<{ answer: RestAnswer<unknown>; ok?: string } | null>(null);

  const call = async (
    method: "POST" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
    ok?: (data: unknown) => string,
  ) => {
    const answer = await rest<unknown>(api, method, path, {
      ...(body === undefined ? {} : { body }),
      profile,
      timeoutMs: 60_000,
    });
    setNote({ answer, ...(answer.ok && ok !== undefined ? { ok: ok(answer.data) } : {}) });
    list.reload();
    return answer;
  };

  const add = async (event: FormEvent) => {
    event.preventDefault();
    const command = draft.command.trim().split(/\s+/).filter(Boolean);
    const body =
      draft.url.trim() !== ""
        ? { name: draft.name.trim(), url: draft.url.trim(), auth: draft.oauth ? "oauth" : "none" }
        : { name: draft.name.trim(), command: command[0] ?? "", args: command.slice(1) };
    const answer = await call("POST", "/api/mcp/servers", body);
    if (answer.ok) setDraft({ name: "", command: "", url: "", oauth: false });
  };

  const signIn = async (name: string) => {
    const answer = await call("POST", `/api/mcp/servers/${encodeURIComponent(name)}/auth`);
    const url = answer.ok
      ? (answer.data as { authorization_url?: unknown }).authorization_url
      : undefined;
    if (typeof url === "string") props.openUrl?.(url);
  };

  return (
    <div className="flex flex-col" data-testid="tab-connectors">
      <Note answer={note?.answer ?? null} {...(note?.ok === undefined ? {} : { ok: note.ok })} />
      <ListState error={list.error} loading={list.rows === null} onRetry={list.reload} />
      <ul className="divide-y divide-line">
        {(list.rows ?? []).map((server) => {
          const remote = typeof server.url === "string" && server.url !== "";
          const path = `/api/mcp/servers/${encodeURIComponent(server.name)}`;
          return (
            <li key={server.name} className="flex items-center gap-2 px-3 py-2 text-sm">
              <Toggle
                label={t("connectors.enabled", { name: server.name })}
                checked={server.enabled !== false}
                onChange={(on) => void call("PUT", `${path}/enabled`, { enabled: on })}
              />
              <span className="flex-1 font-medium">{server.name}</span>
              <span className="text-xs text-muted">
                {t(remote ? "connectors.remote" : "connectors.local")}
              </span>
              <button
                type="button"
                className={BUTTON}
                onClick={() =>
                  void call("POST", `${path}/test`, undefined, (data) =>
                    t("connectors.testOk", {
                      count: ((data as { tools?: unknown[] }).tools ?? []).length,
                    }),
                  )
                }
              >
                {t("connectors.test")}
              </button>
              {remote && server.auth === "oauth" ? (
                <button type="button" className={BUTTON} onClick={() => void signIn(server.name)}>
                  {t("connectors.signIn")}
                </button>
              ) : null}
              <button type="button" className={BUTTON} onClick={() => void call("DELETE", path)}>
                {t("connectors.remove")}
              </button>
            </li>
          );
        })}
      </ul>
      <form className="grid gap-1 border-t border-line px-3 py-2 text-sm" onSubmit={add}>
        <p className="font-medium">{t("connectors.add")}</p>
        {(
          [
            ["name", "connectors.name"],
            ["command", "connectors.command"],
            ["url", "connectors.url"],
          ] as const
        ).map(([key, label]) => (
          <input
            key={key}
            className="rounded border border-line bg-bg px-2 py-0.5"
            aria-label={t(label as MessageKey)}
            placeholder={t(label as MessageKey)}
            value={draft[key]}
            onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
          />
        ))}
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={draft.oauth}
            onChange={(event) => setDraft({ ...draft, oauth: event.target.checked })}
          />
          {t("connectors.oauth")}
        </label>
        <button
          type="submit"
          className={BUTTON}
          disabled={
            draft.name.trim() === "" || (draft.command.trim() === "" && draft.url.trim() === "")
          }
        >
          {t("connectors.save")}
        </button>
      </form>
    </div>
  );
}

// -- plugins --------------------------------------------------------------------------------------

const BADGE_KEYS: Record<PluginBadge, MessageKey> = {
  agent: "plugins.badge.agent",
  desktop: "plugins.badge.desktop",
  both: "plugins.badge.both",
};

export function PluginsTab(props: {
  api: RestLike | null;
  profile: string;
  /** The desktop column (app-level, not reloaded on a profile switch). */
  desktop: readonly DesktopPluginRow[];
}) {
  const t = useT();
  const { api, profile } = props;
  const list = useList<AgentPluginRow>(api, "/api/plugins", profile, field("plugins"));
  const [source, setSource] = useState("");
  const [sha, setSha] = useState("");
  const [note, setNote] = useState<{ answer: RestAnswer<unknown>; ok?: string } | null>(null);
  const [shaError, setShaError] = useState(false);
  const rows = mergePlugins(list.rows ?? [], props.desktop);

  const install = async (event: FormEvent) => {
    event.preventDefault();
    if (!FULL_SHA.test(sha.trim())) {
      setShaError(true);
      return;
    }
    setShaError(false);
    const answer = await rest(api, "POST", "/api/plugins/install", {
      body: { url: source.trim(), sha: sha.trim().toLowerCase() },
      profile,
      timeoutMs: 300_000,
    });
    setNote({ answer });
    if (answer.ok) {
      setSource("");
      setSha("");
    }
    list.reload();
  };

  const toggle = async (name: string, on: boolean) => {
    const answer = await rest(
      api,
      "POST",
      `/api/plugins/${encodeURIComponent(name)}/${on ? "enable" : "disable"}`,
      { profile },
    );
    setNote({ answer });
    list.reload();
  };

  const uninstall = async (name: string) => {
    const answer = await rest(api, "DELETE", `/api/plugins/${encodeURIComponent(name)}`, {
      profile,
    });
    if (!answer.ok && (answer.status === 404 || answer.status === 405)) {
      setNote({ answer: { ...answer, message: t("plugins.uninstallMissing") } });
    } else setNote({ answer });
    list.reload();
  };

  return (
    <div className="flex flex-col" data-testid="tab-plugins">
      <Note answer={note?.answer ?? null} {...(note?.ok === undefined ? {} : { ok: note.ok })} />
      <ListState error={list.error} loading={list.rows === null} onRetry={list.reload} />
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted">
            <th className="px-3 py-1 font-normal" />
            <th className="px-3 py-1 font-normal">{t("plugins.agent")}</th>
            <th className="px-3 py-1 font-normal">{t("plugins.desktop")}</th>
            <th className="px-3 py-1" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.name} className="border-t border-line" data-testid={`plugin-${row.name}`}>
              <td className="px-3 py-2">
                <span className="font-medium">{row.name}</span>{" "}
                <span className="rounded bg-raised px-1 text-xs" data-badge={row.badge}>
                  {t(BADGE_KEYS[row.badge])}
                </span>
              </td>
              <td className="px-3 py-2">
                {row.agent !== null ? (
                  <Toggle
                    label={t("plugins.enabled", { name: row.name })}
                    checked={row.agent.enabled}
                    onChange={(on) => void toggle(row.name, on)}
                  />
                ) : null}
              </td>
              <td className="px-3 py-2 text-xs text-muted">
                {row.desktop !== null ? (row.desktop.version ?? "✓") : null}
              </td>
              <td className="px-3 py-2 text-right">
                {row.agent !== null ? (
                  <button type="button" className={BUTTON} onClick={() => void uninstall(row.name)}>
                    {t("plugins.uninstall")}
                  </button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {list.rows !== null && rows.length === 0 ? (
        <p className="px-3 py-2 text-sm text-muted">{t("capabilities.empty")}</p>
      ) : null}
      <form className="grid gap-1 border-t border-line px-3 py-2 text-sm" onSubmit={install}>
        <p className="font-medium">{t("plugins.install")}</p>
        <input
          className="rounded border border-line bg-bg px-2 py-0.5"
          aria-label={t("plugins.source")}
          placeholder={t("plugins.source")}
          value={source}
          onChange={(event) => setSource(event.target.value)}
        />
        <input
          className="rounded border border-line bg-bg px-2 py-0.5 font-mono"
          aria-label={t("plugins.sha")}
          placeholder={t("plugins.sha")}
          value={sha}
          onChange={(event) => setSha(event.target.value)}
        />
        {shaError ? (
          <p role="alert" className="text-xs text-danger">
            {t("plugins.badSha")}
          </p>
        ) : null}
        <button type="submit" className={BUTTON} disabled={source.trim() === ""}>
          {t("plugins.installButton")}
        </button>
      </form>
    </div>
  );
}
