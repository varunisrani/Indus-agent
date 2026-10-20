// MCP (P18-22): configured MCP servers. Add one (stdio command or HTTP URL), test it (connects,
// lists its tools, disconnects), switch it on or off, or remove it. Env values arrive redacted.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import type {
  McpAuth,
  McpServer,
  McpServerCreate,
  McpServersResponse,
  McpTestResult,
  McpTransport,
} from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { Switch } from "../ui/Switch.tsx";

/** `KEY=value` lines → an env object; null when a line is malformed. */
export function parseEnvLines(text: string): Record<string, string> | null {
  const env: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf("=");
    if (at <= 0) return null;
    const key = line.slice(0, at).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return null;
    env[key] = line.slice(at + 1);
  }
  return env;
}

/** Split an argument line on whitespace, keeping "quoted parts" together. */
export function splitArgs(text: string): string[] {
  const out: string[] = [];
  for (const match of text.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g))
    out.push(match[1] ?? match[2] ?? match[3] ?? "");
  return out;
}

function AddServer() {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<McpTransport>("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [envText, setEnvText] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState<McpAuth>("none");
  const [problem, setProblem] = useState("");

  const add = useMutation({
    mutationFn: (body: McpServerCreate) => api.mcp.add(body),
    onSuccess: () => {
      toast.show(`${name} added.`, "success");
      setName("");
      setCommand("");
      setArgs("");
      setEnvText("");
      setUrl("");
      setProblem("");
      void client.invalidateQueries({ queryKey: ["mcp"] });
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(name.trim()))
      return setProblem("Name the server with letters, digits, dots, dashes or underscores.");
    let body: McpServerCreate;
    if (transport === "stdio") {
      if (!command.trim()) return setProblem("Give the command that starts the server.");
      const env = parseEnvLines(envText);
      if (env === null)
        return setProblem("Write environment variables as KEY=value, one per line.");
      body = { name: name.trim(), command: command.trim(), args: splitArgs(args), env };
    } else {
      if (!/^https?:\/\/\S+$/.test(url.trim())) return setProblem("Give the server's http(s) URL.");
      body = { name: name.trim(), url: url.trim(), auth };
    }
    setProblem("");
    add.mutate(body);
  };

  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold">Add a server</h2>
      <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
        <Field label="Name" htmlFor={`${ids}-name`}>
          <input
            id={`${ids}-name`}
            className={inputClass}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <Field label="Transport" htmlFor={`${ids}-transport`}>
          <select
            id={`${ids}-transport`}
            className={inputClass}
            value={transport}
            onChange={(e) => {
              const next = e.currentTarget.value as McpTransport;
              setTransport(next);
              if (next === "stdio") {
                setUrl("");
                setAuth("none");
              } else {
                setCommand("");
                setArgs("");
                setEnvText("");
              }
            }}
          >
            <option value="stdio">stdio (a local command)</option>
            <option value="http">HTTP (a URL)</option>
          </select>
        </Field>
        {transport === "stdio" ? (
          <>
            <Field label="Command" htmlFor={`${ids}-command`}>
              <input
                id={`${ids}-command`}
                className={`${inputClass} font-mono`}
                value={command}
                onChange={(e) => setCommand(e.currentTarget.value)}
              />
            </Field>
            <Field label="Arguments" htmlFor={`${ids}-args`}>
              <input
                id={`${ids}-args`}
                className={`${inputClass} font-mono`}
                value={args}
                onChange={(e) => setArgs(e.currentTarget.value)}
              />
            </Field>
            <Field label="Environment (KEY=value per line)" htmlFor={`${ids}-env`}>
              <textarea
                id={`${ids}-env`}
                className={`${inputClass} font-mono`}
                rows={3}
                value={envText}
                onChange={(e) => setEnvText(e.currentTarget.value)}
              />
            </Field>
          </>
        ) : (
          <>
            <Field label="URL" htmlFor={`${ids}-url`}>
              <input
                id={`${ids}-url`}
                className={inputClass}
                value={url}
                onChange={(e) => setUrl(e.currentTarget.value)}
              />
            </Field>
            <Field label="Authentication" htmlFor={`${ids}-auth`}>
              <select
                id={`${ids}-auth`}
                className={inputClass}
                value={auth}
                onChange={(e) => setAuth(e.currentTarget.value as McpAuth)}
              >
                <option value="none">None</option>
                <option value="header">Header (set the key on the Keys page)</option>
                <option value="oauth">OAuth sign-in</option>
              </select>
            </Field>
          </>
        )}
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div>
          <Button type="submit" tone="primary" disabled={add.isPending}>
            Add server
          </Button>
        </div>
      </form>
    </Card>
  );
}

function TestResult({ result }: { result: McpTestResult }) {
  if (!result.ok)
    return <Notice tone="danger">Test failed: {result.error ?? "no reason given"}</Notice>;
  return (
    <div className="rounded-md border border-ok p-2 text-sm">
      <p className="mb-1 text-ok">
        Connected. {result.tools.length} tool{result.tools.length === 1 ? "" : "s"}:
      </p>
      <ul aria-label="Server tools" className="flex flex-col gap-0.5">
        {result.tools.map((tool) => (
          <li key={tool.name}>
            <code className="font-mono text-xs">{tool.name}</code>
            {tool.description ? (
              <span className="text-xs text-muted"> — {tool.description}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ServerRow({ server, onRemove }: { server: McpServer; onRemove: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const [result, setResult] = useState<McpTestResult | null>(null);
  const test = useMutation({
    mutationFn: () => api.mcp.test(server.name),
    onSuccess: setResult,
    onError: (error) => setResult({ ok: false, error: errorText(error), tools: [] }),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.mcp.setEnabled(server.name, enabled),
    onSuccess: (_reply, enabled) =>
      client.setQueryData<McpServersResponse>(["mcp", "servers"], (data) =>
        data
          ? { servers: data.servers.map((s) => (s.name === server.name ? { ...s, enabled } : s)) }
          : data,
      ),
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const envKeys = Object.entries(server.env);
  return (
    <li
      className="flex flex-col gap-2 rounded-md border border-line bg-raised p-3"
      data-testid={`mcp-${server.name}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 text-sm font-medium">{server.name}</span>
        <Badge tone={server.transport === "http" ? "accent" : "neutral"}>{server.transport}</Badge>
        <Switch
          checked={server.enabled}
          onCheckedChange={(enabled) => toggle.mutate(enabled)}
          label={`Enable ${server.name}`}
        />
      </div>
      <code className="font-mono text-xs break-all">
        {server.transport === "http" ? server.url : [server.command, ...server.args].join(" ")}
      </code>
      {envKeys.length ? (
        <p className="font-mono text-xs text-muted">
          {envKeys.map(([key, value]) => `${key}=${value}`).join("  ")}
        </p>
      ) : null}
      <div className="flex gap-1">
        <Button onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? "Testing…" : "Test"}
        </Button>
        <Button tone="danger" onClick={onRemove}>
          Remove
        </Button>
      </div>
      {result ? <TestResult result={result} /> : null}
    </li>
  );
}

export function McpPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [removing, setRemoving] = useState<string | null>(null);
  const servers = useQuery({ queryKey: ["mcp", "servers"], queryFn: () => api.mcp.servers() });
  const remove = useMutation({
    mutationFn: (name: string) => api.mcp.remove(name),
    onSuccess: (_reply, name) => {
      setRemoving(null);
      toast.show(`${name} removed.`, "success");
      void client.invalidateQueries({ queryKey: ["mcp"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const list = servers.data?.servers ?? [];
  return (
    <div className="flex flex-col gap-4 p-4">
      <QueryState
        loading={servers.isPending}
        error={servers.error}
        empty={list.length === 0}
        emptyText="No MCP servers configured."
      >
        <ul className="flex flex-col gap-2" aria-label="MCP servers">
          {list.map((server) => (
            <ServerRow
              key={server.name}
              server={server}
              onRemove={() => setRemoving(server.name)}
            />
          ))}
        </ul>
      </QueryState>
      <AddServer />
      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing ?? ""}?`}
        description="The server is removed from this profile's configuration; its tools disappear from new sessions."
        confirmLabel="Remove"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (removing) remove.mutate(removing);
        }}
        onCancel={() => setRemoving(null)}
      />
    </div>
  );
}
