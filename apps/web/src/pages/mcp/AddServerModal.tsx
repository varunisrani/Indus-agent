// The "Add MCP server" panel (W52-c): name, transport, then either a URL with a sign-in mode or a
// command line with arguments and environment. Problems are shown inline before anything is sent.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import type { McpAuth, McpServerCreate, McpTransport } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Button, Field, Input, Notice, Select, Textarea } from "../../ui/controls.tsx";
import { DialogActions, Modal } from "../../ui/Dialog.tsx";

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

const AUTH_HINT: Partial<Record<McpAuth, string>> = {
  header: "Store the token on the Keys page; the server entry only names it.",
  oauth: "You sign in from this page once the server is added.",
};

export function AddServerModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<McpTransport>("http");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [envText, setEnvText] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState<McpAuth>("none");
  const [problem, setProblem] = useState("");

  const reset = () => {
    setName("");
    setCommand("");
    setArgs("");
    setEnvText("");
    setUrl("");
    setAuth("none");
    setProblem("");
  };

  const add = useMutation({
    mutationFn: (body: McpServerCreate) => api.mcp.add(body),
    onSuccess: (_reply, body) => {
      toast.show(`${body.name} added.`, "success");
      reset();
      void client.invalidateQueries({ queryKey: ["mcp"] });
      onClose();
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

  const switchTransport = (next: McpTransport) => {
    setTransport(next);
    if (next === "stdio") {
      setUrl("");
      setAuth("none");
    } else {
      setCommand("");
      setArgs("");
      setEnvText("");
    }
  };

  return (
    <Modal
      open={open}
      size="narrow"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title="Add MCP server"
    >
      <form className="flex flex-col gap-4 py-1" onSubmit={submit} noValidate>
        <Field label="Name" htmlFor={`${ids}-name`}>
          <Input
            id={`${ids}-name`}
            autoFocus
            placeholder="my-server"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <Field label="Transport" htmlFor={`${ids}-transport`}>
          <Select
            id={`${ids}-transport`}
            value={transport}
            onChange={(e) => switchTransport(e.currentTarget.value as McpTransport)}
          >
            <option value="http">HTTP/SSE</option>
            <option value="stdio">stdio</option>
          </Select>
        </Field>
        {transport === "stdio" ? (
          <>
            <Field label="Command" htmlFor={`${ids}-command`}>
              <Input
                id={`${ids}-command`}
                placeholder="npx"
                value={command}
                onChange={(e) => setCommand(e.currentTarget.value)}
              />
            </Field>
            <Field label="Arguments" htmlFor={`${ids}-args`}>
              <Input
                id={`${ids}-args`}
                placeholder="-y my-mcp-server --flag"
                value={args}
                onChange={(e) => setArgs(e.currentTarget.value)}
              />
            </Field>
            <Field label="Environment (KEY=value per line)" htmlFor={`${ids}-env`}>
              <Textarea
                id={`${ids}-env`}
                className="min-h-20"
                rows={3}
                value={envText}
                onChange={(e) => setEnvText(e.currentTarget.value)}
              />
            </Field>
          </>
        ) : (
          <>
            <Field label="URL" htmlFor={`${ids}-url`}>
              <Input
                id={`${ids}-url`}
                placeholder="https://example.com/mcp"
                value={url}
                onChange={(e) => setUrl(e.currentTarget.value)}
              />
            </Field>
            <Field
              label="Authentication"
              htmlFor={`${ids}-auth`}
              {...(AUTH_HINT[auth] ? { hint: AUTH_HINT[auth] } : {})}
            >
              <Select
                id={`${ids}-auth`}
                value={auth}
                onChange={(e) => setAuth(e.currentTarget.value as McpAuth)}
              >
                <option value="none">None</option>
                <option value="header">Bearer token</option>
                <option value="oauth">OAuth</option>
              </Select>
            </Field>
          </>
        )}
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <DialogActions>
          <Button
            type="submit"
            tone="primary"
            size="sm"
            className="uppercase"
            disabled={add.isPending}
          >
            {add.isPending ? "Adding…" : "Add"}
          </Button>
        </DialogActions>
      </form>
    </Modal>
  );
}
