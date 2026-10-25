// MCP (P18-22, look W52-c): configured MCP servers and a catalogue section. Add one (HTTP URL or a
// stdio command) from the header, test it (connects, lists its tools, disconnects), switch it on or
// off, or remove it. Env values arrive redacted. The catalogue has no backend yet: it shows an empty
// section that "Browse catalog" scrolls to.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Package, Power, Server, Trash2, Zap } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "../lib/api.ts";
import type { McpServer, McpServersResponse, McpTestResult } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  Button,
  Card,
  ErrorState,
  IconButton,
  Loading,
  SectionHeading,
} from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { AddServerModal } from "./mcp/AddServerModal.tsx";

export { parseEnvLines, splitArgs } from "./mcp/AddServerModal.tsx";

function TestResult({ result }: { result: McpTestResult }) {
  if (!result.ok)
    return (
      <p role="alert" className="m-0 text-[0.75rem] text-danger">
        Test failed: {result.error ?? "no reason given"}
      </p>
    );
  if (result.tools.length === 0)
    return <p className="m-0 text-[0.75rem] text-success">Connected, no tools.</p>;
  return (
    <div className="text-[0.75rem] text-success">
      Tools:{" "}
      <ul aria-label="Server tools" className="m-0 inline list-none p-0">
        {result.tools.map((tool, i) => (
          <li key={tool.name} className="inline">
            <code className="type-data text-[0.75rem]" title={tool.description || undefined}>
              {tool.name}
            </code>
            {i < result.tools.length - 1 ? ", " : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ServerCard({
  server,
  onRemove,
  onToggled,
}: {
  server: McpServer;
  onRemove: () => void;
  onToggled: () => void;
}) {
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
    onSuccess: (_reply, enabled) => {
      client.setQueryData<McpServersResponse>(["mcp", "servers"], (data) =>
        data
          ? { servers: data.servers.map((s) => (s.name === server.name ? { ...s, enabled } : s)) }
          : data,
      );
      onToggled();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const env = Object.entries(server.env);
  const target =
    server.transport === "http" ? server.url : [server.command, ...server.args].join(" ");
  return (
    <li
      className={cx(
        "flex items-start gap-3 border border-line px-4 py-3.5 transition-opacity",
        !server.enabled && "opacity-60",
      )}
      data-testid={`mcp-${server.name}`}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.875rem] font-medium text-ink">{server.name}</span>
          <Badge tone={server.transport === "http" ? "ok" : "warn"}>{server.transport}</Badge>
          {server.auth !== "none" ? <Badge>auth: {server.auth}</Badge> : null}
          {server.enabled ? null : <Badge>disabled</Badge>}
        </div>
        <p className="m-0 flex min-w-0 flex-wrap gap-x-2 text-[0.75rem] text-ink-muted">
          <code className="type-data min-w-0 truncate text-[0.75rem]">{target}</code>
          {env.length ? (
            <span title={env.map(([key, value]) => `${key}=${value}`).join("\n")}>
              · {env.length} env var{env.length === 1 ? "" : "s"}
            </span>
          ) : null}
        </p>
        {result ? <TestResult result={result} /> : null}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          tone="ghost"
          size="sm"
          icon={<Power className={server.enabled ? "text-success" : undefined} />}
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(!server.enabled)}
        >
          {server.enabled ? "Disable" : "Enable"}
        </Button>
        <IconButton label="Test" disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? <LoaderCircle className="animate-spin" /> : <Zap />}
        </IconButton>
        <IconButton label="Remove" tone="danger" onClick={onRemove}>
          <Trash2 />
        </IconButton>
      </div>
    </li>
  );
}

export function McpPage() {
  const toast = useToast();
  const client = useQueryClient();
  const catalog = useRef<HTMLElement>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [restartNote, setRestartNote] = useState(false);
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
  const browse = () => catalog.current?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className="flex flex-col gap-6 px-4 py-[1.4375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderActions>
        <Button tone="primary" size="sm" className="uppercase" onClick={() => setAdding(true)}>
          Add server
        </Button>
      </HeaderActions>

      <section aria-label="Your MCP servers" className="flex flex-col gap-3">
        <SectionHeading icon={Server}>Your MCP servers ({list.length})</SectionHeading>
        {restartNote ? (
          <p className="m-0 text-[0.75rem] text-warning">
            Changes reach the agent in sessions started after a restart.
          </p>
        ) : null}
        {servers.isPending ? (
          <div className="flex justify-center py-12">
            <Loading />
          </div>
        ) : servers.error ? (
          <ErrorState message={errorText(servers.error)} onRetry={() => void servers.refetch()} />
        ) : list.length === 0 ? (
          <Card className="flex flex-col items-center gap-3 py-8 text-center">
            <p className="m-0 text-[0.8125rem] text-ink-muted">
              No MCP servers yet. A server gives the agent extra tools, such as access to a code
              host, a database or a browser. Add one from the catalog below or with Add server at
              the top of the page.
            </p>
            <Button tone="primary" size="sm" icon={<Package />} onClick={browse}>
              Browse catalog
            </Button>
          </Card>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="MCP servers">
            {list.map((server) => (
              <ServerCard
                key={server.name}
                server={server}
                onRemove={() => setRemoving(server.name)}
                onToggled={() => setRestartNote(true)}
              />
            ))}
          </ul>
        )}
      </section>

      <section ref={catalog} aria-label="Catalog" className="flex scroll-mt-4 flex-col gap-3">
        <div className="flex flex-col gap-2">
          <SectionHeading icon={Package}>Catalog (0)</SectionHeading>
          <p className="m-0 text-[0.75rem] text-ink-muted">
            Reviewed servers you can add in one step will be listed here.
          </p>
        </div>
        <Card className="py-6 text-center">
          <p className="m-0 text-[0.8125rem] text-ink-muted">No catalog entries available.</p>
        </Card>
      </section>

      <AddServerModal open={adding} onClose={() => setAdding(false)} />
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
