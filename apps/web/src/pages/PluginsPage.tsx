// Plugins (P18-22): installed agent plugins with enable/disable, and install from a git URL pinned
// to a full 40-character commit (checked here before any request is sent).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type { PluginsResponse } from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../ui/controls.tsx";
import { Switch } from "../ui/Switch.tsx";

const FULL_SHA = /^[0-9a-f]{40}$/;

/** Why a plugin install request would be refused, or null when it can be sent. */
export function installProblem(url: string, sha: string): string | null {
  const repo = url.trim();
  if (!/^(https:\/\/|ssh:\/\/|git@)\S+$/.test(repo))
    return "Enter the plugin's git URL (https://, ssh:// or git@).";
  if (!FULL_SHA.test(sha.trim().toLowerCase())) {
    return "Pin the install to a full 40-character commit SHA (a short SHA, branch or tag is not accepted).";
  }
  return null;
}

export function PluginsPage() {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [url, setUrl] = useState("");
  const [sha, setSha] = useState("");
  const [problem, setProblem] = useState("");
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
  const install = useMutation({
    mutationFn: () => api.plugins.install({ url: url.trim(), sha: sha.trim().toLowerCase() }),
    onSuccess: () => {
      setUrl("");
      setSha("");
      toast.show("Plugin installed. Enable it to use it.", "success");
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

  const list = plugins.data?.plugins ?? [];
  return (
    <div className="flex flex-col gap-4 p-4">
      <Card>
        <h2 className="mb-3 text-sm font-semibold">Install from git</h2>
        <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
          <Field label="Git URL" htmlFor={`${ids}-url`}>
            <input
              id={`${ids}-url`}
              className={inputClass}
              value={url}
              onChange={(e) => setUrl(e.currentTarget.value)}
              placeholder="https://example.org/team/plugin.git"
            />
          </Field>
          <Field
            label="Commit SHA"
            htmlFor={`${ids}-sha`}
            hint="The full 40-character commit; installs are pinned so an upstream change cannot slip in."
          >
            <input
              id={`${ids}-sha`}
              className={`${inputClass} font-mono`}
              value={sha}
              onChange={(e) => setSha(e.currentTarget.value)}
            />
          </Field>
          {problem ? <Notice tone="danger">{problem}</Notice> : null}
          <div>
            <Button type="submit" tone="primary" disabled={install.isPending}>
              Install
            </Button>
          </div>
        </form>
      </Card>
      <QueryState
        loading={plugins.isPending}
        error={plugins.error}
        empty={list.length === 0}
        emptyText="No plugins installed."
      >
        <ul className="flex flex-col gap-2" aria-label="Installed plugins">
          {list.map((plugin) => (
            <li
              key={plugin.name}
              className="flex items-center gap-3 rounded-md border border-line bg-raised p-3"
            >
              <div className="flex flex-1 flex-col">
                <span className="text-sm font-medium">
                  {plugin.name}{" "}
                  {plugin.version ? (
                    <span className="text-xs text-muted">v{plugin.version}</span>
                  ) : null}
                </span>
                <span className="text-xs text-muted">{plugin.description}</span>
                {plugin.source ? (
                  <span className="font-mono text-xs text-muted">
                    {plugin.source}
                    {plugin.sha ? ` @ ${plugin.sha.slice(0, 12)}` : ""}
                  </span>
                ) : null}
              </div>
              <Badge tone={plugin.enabled ? "ok" : "neutral"}>
                {plugin.enabled ? "enabled" : "disabled"}
              </Badge>
              <Switch
                checked={plugin.enabled}
                onCheckedChange={(enabled) => toggle.mutate({ name: plugin.name, enabled })}
                label={`Enable ${plugin.name}`}
              />
            </li>
          ))}
        </ul>
      </QueryState>
    </div>
  );
}
