// Plugins (P18-22, look W52-c): runtime providers (memory provider, context engine), install from
// git pinned to a full 40-character commit (checked here before any request is sent), a catalogue
// section (no backend yet) and the installed plugins with enable/disable.

import { BRAND_NAME } from "@indus/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { type FormEvent, type ReactNode, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type { ConfigObject, ConfigValue, PluginInfo, PluginsResponse } from "../lib/restTypes.ts";
import { HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  Button,
  Card,
  Field,
  IconButton,
  Input,
  Loading,
  Notice,
  QueryState,
  Select,
} from "../ui/controls.tsx";
import { Switch } from "../ui/Switch.tsx";

const FULL_SHA = /^[0-9a-f]{40}$/;
const GIT_URL = /^(https:\/\/|ssh:\/\/|git@)\S+$/;
const OWNER_REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\/[A-Za-z0-9_.\-/]+)?$/;

/** Why a plugin install request would be refused, or null when it can be sent. */
export function installProblem(url: string, sha: string): string | null {
  const repo = url.trim();
  if (!GIT_URL.test(repo) && !OWNER_REPO.test(repo))
    return "Enter owner/repo or the plugin's git URL (https://, ssh:// or git@).";
  if (!FULL_SHA.test(sha.trim().toLowerCase())) {
    return "Pin the install to a full 40-character commit SHA (a short SHA, branch or tag is not accepted).";
  }
  return null;
}

/** `context.engine` from a config object; "builtin" when unset. */
function contextEngine(config: ConfigObject | undefined): string {
  const context = config?.context;
  if (context && typeof context === "object" && !Array.isArray(context)) {
    const engine = (context as Record<string, ConfigValue>).engine;
    if (typeof engine === "string" && engine) return engine;
  }
  return "builtin";
}

function CardHead({ title, hint }: { title: string; hint: string }) {
  return (
    <header className="border-b border-line px-4 py-3.5">
      <h2 className="type-section-title m-0 text-[0.9375rem]">{title}</h2>
      <p className="m-0 mt-1 text-[0.6875rem] tracking-[0.08em] text-ink-soft">{hint}</p>
    </header>
  );
}

function RuntimeProviders() {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const memory = useQuery({ queryKey: ["system", "memory"], queryFn: () => api.system.memory() });
  const config = useQuery({ queryKey: ["config", "file"], queryFn: () => api.config.get() });
  const [provider, setProvider] = useState<string | null>(null);
  const [engine, setEngine] = useState<string | null>(null);

  const active = memory.data?.active ?? "";
  const chosen = provider ?? active;
  const currentEngine = contextEngine(config.data);
  const chosenEngine = engine ?? currentEngine;

  const saveMemory = useMutation({
    mutationFn: () => api.system.setMemoryProvider(chosen),
    onSuccess: () => {
      toast.show("Memory provider saved. New sessions use it.", "success");
      setProvider(null);
      void client.invalidateQueries({ queryKey: ["system", "memory"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const saveEngine = useMutation({
    mutationFn: () => {
      const base = config.data ?? {};
      const context = base.context;
      const section =
        context && typeof context === "object" && !Array.isArray(context)
          ? (context as Record<string, ConfigValue>)
          : {};
      return api.config.put({ ...base, context: { ...section, engine: chosenEngine } });
    },
    onSuccess: () => {
      toast.show("Context engine saved. New sessions use it.", "success");
      setEngine(null);
      void client.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const engines = currentEngine === "builtin" ? ["builtin"] : ["builtin", currentEngine];
  return (
    <Card padded={false} aria-label="Runtime provider plugins">
      <CardHead
        title="Runtime provider plugins"
        hint="Choose the memory provider and the context engine new sessions run with."
      />
      <div className="grid grid-cols-1 gap-6 p-4 lg:grid-cols-[1.35fr_minmax(16.25rem,0.65fr)]">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <label htmlFor={`${ids}-memory`} className="type-label text-ink-muted">
              Memory provider
            </label>
            {memory.data && chosen === active ? <Badge tone="ok">active</Badge> : null}
          </div>
          <Select
            id={`${ids}-memory`}
            value={chosen}
            disabled={!memory.data}
            onChange={(e) => setProvider(e.currentTarget.value)}
          >
            <option value="">(built-in / default)</option>
            {(memory.data?.providers ?? []).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
          <p className="m-0 text-[0.75rem] text-ink-muted">
            {chosen
              ? `${BRAND_NAME} stores memories through the ${chosen} provider.`
              : `${BRAND_NAME} uses its built-in memory files${
                  memory.data ? ` (${Object.keys(memory.data.builtin_files).join(", ")})` : ""
                }.`}
          </p>
          {memory.error ? <Notice tone="danger">{errorText(memory.error)}</Notice> : null}
          <div>
            <Button
              tone="primary"
              size="sm"
              className="uppercase"
              disabled={!memory.data || saveMemory.isPending}
              onClick={() => saveMemory.mutate()}
            >
              Save memory provider
            </Button>
          </div>
        </div>
        <div className="flex flex-col gap-3">
          <label htmlFor={`${ids}-engine`} className="type-label text-ink-muted">
            Context engine
          </label>
          <Select
            id={`${ids}-engine`}
            value={chosenEngine}
            disabled={!config.data}
            onChange={(e) => setEngine(e.currentTarget.value)}
          >
            {engines.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
          <div>
            <Button
              tone="primary"
              size="sm"
              className="uppercase"
              disabled={!config.data || saveEngine.isPending}
              onClick={() => saveEngine.mutate()}
            >
              Save context engine
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}

function InstallCard() {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [url, setUrl] = useState("");
  const [sha, setSha] = useState("");
  const [force, setForce] = useState(false);
  const [enableAfter, setEnableAfter] = useState(true);
  const [problem, setProblem] = useState("");

  const install = useMutation({
    mutationFn: async () => {
      const reply = await api.plugins.install({
        url: url.trim(),
        sha: sha.trim().toLowerCase(),
        force,
      });
      if (enableAfter && reply.name) await api.plugins.setEnabled(reply.name, true);
      return reply;
    },
    onSuccess: (reply) => {
      setUrl("");
      setSha("");
      setProblem("");
      toast.show(
        enableAfter && reply.name
          ? `${reply.name} installed and enabled.`
          : "Plugin installed. Enable it to use it.",
        "success",
      );
      void client.invalidateQueries({ queryKey: ["plugins"] });
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const reason = installProblem(url, sha);
    setProblem(reason ?? "");
    if (reason === null) install.mutate();
  };

  return (
    <Card padded={false} aria-label="Install from GitHub / Git URL">
      <CardHead
        title="Install from GitHub / Git URL"
        hint="Give owner/repo, owner/repo/path for a plugin in a subfolder, or a full https://, ssh:// or git@ clone URL."
      />
      <form className="flex flex-col gap-4 p-4" onSubmit={submit} noValidate>
        <Field label="Git URL or owner/repo" htmlFor={`${ids}-url`}>
          <Input
            id={`${ids}-url`}
            value={url}
            onChange={(e) => setUrl(e.currentTarget.value)}
            placeholder="owner/repo, owner/repo/subdir, or https://..."
          />
        </Field>
        <Field
          label="Commit SHA"
          htmlFor={`${ids}-sha`}
          hint="The full 40-character commit; installs are pinned so an upstream change cannot slip in."
        >
          <Input id={`${ids}-sha`} value={sha} onChange={(e) => setSha(e.currentTarget.value)} />
        </Field>
        <div className="flex flex-wrap items-center gap-x-8 gap-y-2">
          <SwitchRow
            id={`${ids}-force`}
            checked={force}
            onChange={setForce}
            label="Force reinstall (replace the installed copy)"
          />
          <SwitchRow
            id={`${ids}-enable`}
            checked={enableAfter}
            onChange={setEnableAfter}
            label="Enable after install"
          />
        </div>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div>
          <Button
            type="submit"
            tone="primary"
            size="sm"
            className="uppercase"
            disabled={install.isPending}
            icon={install.isPending ? <LoaderCircle className="animate-spin" /> : undefined}
          >
            Install
          </Button>
        </div>
        <p className="m-0 text-[0.6875rem] tracking-[0.04em] text-ink-soft">
          Added plugin files by hand? Use the refresh button by the title so the list picks them up.
        </p>
        <p className="m-0 text-[0.6875rem] tracking-[0.04em] text-ink-soft">
          Every install is scanned first; a plugin the scanner rates dangerous is refused.
        </p>
      </form>
    </Card>
  );
}

function SwitchRow({
  id,
  checked,
  onChange,
  label,
}: {
  id: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <span className="flex items-center gap-2.5">
      <Switch id={id} checked={checked} onCheckedChange={onChange} label={label} />
      <label htmlFor={id} className="text-[0.75rem] tracking-[0.04em] text-ink-muted">
        {label}
      </label>
    </span>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-3">
      <h2 className="type-label m-0 text-ink-muted">{title}</h2>
      {children}
    </section>
  );
}

function PluginCard({
  plugin,
  busy,
  onToggle,
}: {
  plugin: PluginInfo;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <li
      className="flex items-start gap-3 border border-line px-4 py-3.5"
      data-testid={`plugin-${plugin.name}`}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.9375rem] font-bold text-ink">{plugin.name}</span>
          {plugin.source ? (
            <Badge className="max-w-[24rem] truncate">source: {plugin.source}</Badge>
          ) : null}
          {plugin.version ? <Badge>v{plugin.version}</Badge> : null}
          <Badge tone={plugin.enabled ? "ok" : "danger"}>
            {plugin.enabled ? "enabled" : "disabled"}
          </Badge>
        </div>
        {plugin.description ? (
          <p className="m-0 text-[0.75rem] text-ink-muted">{plugin.description}</p>
        ) : null}
        {plugin.sha ? (
          <code className="type-data text-[0.6875rem] text-ink-soft">
            @ {plugin.sha.slice(0, 12)}
          </code>
        ) : null}
      </div>
      <Button tone="ghost" size="sm" disabled={busy} onClick={onToggle}>
        {plugin.enabled ? "Disable" : "Enable"}
      </Button>
    </li>
  );
}

export function PluginsPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [catalogQuery, setCatalogQuery] = useState("");
  const plugins = useQuery({ queryKey: ["plugins"], queryFn: () => api.plugins.list() });

  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      api.plugins.setEnabled(name, enabled),
    onSuccess: (_reply, { name, enabled }) => {
      client.setQueryData<PluginsResponse>(["plugins"], (data) =>
        data
          ? { plugins: data.plugins.map((p) => (p.name === name ? { ...p, enabled } : p)) }
          : data,
      );
      toast.show(
        `${name} ${enabled ? "enabled" : "disabled"}. It takes effect in new sessions.`,
        "success",
      );
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const refreshing = plugins.isFetching;
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["plugins"] });
    void client.invalidateQueries({ queryKey: ["system", "memory"] });
    void client.invalidateQueries({ queryKey: ["config", "file"] });
  };

  const list = plugins.data?.plugins ?? [];
  return (
    <div className="flex flex-col gap-8 px-4 py-[1.4375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderAfterTitle>
        <IconButton label="Refresh plugins" disabled={refreshing} onClick={refresh}>
          {refreshing ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}
        </IconButton>
      </HeaderAfterTitle>
      <RuntimeProviders />
      <InstallCard />
      <Section title="Plugin catalog">
        <p className="m-0 -mt-1 text-[0.75rem] tracking-[0.04em] text-ink-soft">
          Reviewed plugins pinned to exact commits will be listed here for a one-step install.
        </p>
        <Input
          type="search"
          aria-label="Search catalog"
          placeholder="Search catalog..."
          className="w-full max-w-[28rem]"
          value={catalogQuery}
          onChange={(e) => setCatalogQuery(e.currentTarget.value)}
        />
        <p className="m-0 text-[0.75rem] text-ink-soft">No results</p>
      </Section>
      <Section title="Installed plugins">
        {plugins.isPending ? (
          <Loading />
        ) : (
          <QueryState
            loading={false}
            error={plugins.error}
            empty={list.length === 0}
            emptyText="No plugins installed."
            onRetry={() => void plugins.refetch()}
          >
            <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Installed plugins">
              {list.map((plugin) => (
                <PluginCard
                  key={plugin.name}
                  plugin={plugin}
                  busy={toggle.isPending}
                  onToggle={() => toggle.mutate({ name: plugin.name, enabled: !plugin.enabled })}
                />
              ))}
            </ul>
          </QueryState>
        )}
      </Section>
    </div>
  );
}
