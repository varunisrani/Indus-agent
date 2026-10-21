// Webhooks (P18-23): the gateway's webhook receiver for this profile. Routes added here get a fresh
// signing secret that the create answer carries once; this page keeps it only in its own state, so
// it is gone after navigating away, and no list answer ever has it. Routes written in config.yaml
// are listed read-only.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import {
  WEBHOOK_AUTHS,
  type WebhookAuth,
  type WebhookCreate,
  type WebhookRoute,
  type WebhooksResponse,
} from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { CopyButton } from "../ui/CopyButton.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { Switch } from "../ui/Switch.tsx";

const AUTH_LABEL: Record<WebhookAuth, string> = {
  auto: "Automatic (detect the sender's signature)",
  github: "GitHub signature",
  gitlab: "GitLab token",
  hmac: "HMAC signature",
  hmac_ts: "HMAC signature with timestamp",
};

/** Comma- or line-separated event names → a clean list. */
export function parseEvents(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

interface Created {
  name: string;
  url: string;
  secret: string;
}

function CreatedRoute({ created, onDone }: { created: Created; onDone: () => void }) {
  return (
    <section
      aria-label="New webhook route"
      className="flex flex-col gap-2 rounded-md border border-ok p-3 text-sm"
    >
      <p className="font-medium">Route {created.name} created.</p>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">URL</span>
        <code className="flex-1 font-mono text-xs break-all">{created.url}</code>
        <CopyButton value={created.url} label="Copy the route URL" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">Signing secret</span>
        <code data-testid="webhook-secret" className="flex-1 font-mono text-xs break-all">
          {created.secret}
        </code>
        <CopyButton value={created.secret} label="Copy the signing secret" />
      </div>
      <Notice tone="warn">
        Copy the secret now and give it to the sender. You will not see it again: the dashboard
        keeps only its name.
      </Notice>
      <div>
        <Button onClick={onDone}>I have copied it</Button>
      </div>
    </section>
  );
}

function CreateRoute({ onCreated }: { onCreated: (created: Created) => void }) {
  const client = useQueryClient();
  const ids = useId();
  const [name, setName] = useState("");
  const [events, setEvents] = useState("");
  const [prompt, setPrompt] = useState("");
  const [deliver, setDeliver] = useState("");
  const [deliverOnly, setDeliverOnly] = useState(false);
  const [auth, setAuth] = useState<WebhookAuth>("auto");
  const [description, setDescription] = useState("");
  const [problem, setProblem] = useState("");

  const create = useMutation({
    mutationFn: (body: WebhookCreate) => api.webhooks.create(body),
    // The answer holds the secret: do not keep it in the mutation cache after this page goes.
    gcTime: 0,
    onSuccess: (reply) => {
      onCreated({ name: reply.name, url: reply.url, secret: reply.secret });
      setName("");
      setEvents("");
      setPrompt("");
      setDeliver("");
      setDeliverOnly(false);
      setAuth("auto");
      setDescription("");
      setProblem("");
      void client.invalidateQueries({ queryKey: ["webhooks"] });
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const clean = name.trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(clean)) {
      setProblem("Name the route with lower-case letters, digits, '-' or '_'.");
      return;
    }
    if (deliverOnly && !deliver.trim()) {
      setProblem("Direct delivery needs a delivery target.");
      return;
    }
    setProblem("");
    const body: WebhookCreate = { name: clean, auth, deliver_only: deliverOnly };
    const list = parseEvents(events);
    if (list.length) body.events = list;
    if (prompt.trim()) body.prompt = prompt;
    if (deliver.trim()) body.deliver = deliver.trim();
    if (description.trim()) body.description = description.trim();
    create.mutate(body);
  };

  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold">Add a route</h2>
      <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
        <Field label="Name" htmlFor={`${ids}-name`} hint="Part of the URL: /webhooks/<name>.">
          <input
            id={`${ids}-name`}
            className={inputClass}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <Field
          label="Events"
          htmlFor={`${ids}-events`}
          hint="Comma-separated; leave empty to accept every event."
        >
          <input
            id={`${ids}-events`}
            className={inputClass}
            value={events}
            onChange={(e) => setEvents(e.currentTarget.value)}
          />
        </Field>
        <Field
          label="Prompt"
          htmlFor={`${ids}-prompt`}
          hint="What the agent is asked; the payload is added."
        >
          <textarea
            id={`${ids}-prompt`}
            className={inputClass}
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.currentTarget.value)}
          />
        </Field>
        <Field
          label="Deliver to"
          htmlFor={`${ids}-deliver`}
          hint="A delivery target such as telegram:<chat id>; empty keeps the answer in the log."
        >
          <input
            id={`${ids}-deliver`}
            className={inputClass}
            value={deliver}
            onChange={(e) => setDeliver(e.currentTarget.value)}
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={deliverOnly}
            onChange={(e) => setDeliverOnly(e.currentTarget.checked)}
          />
          Deliver the payload directly, without the agent
        </label>
        <Field label="Signature check" htmlFor={`${ids}-auth`}>
          <select
            id={`${ids}-auth`}
            className={inputClass}
            value={auth}
            onChange={(e) => setAuth(e.currentTarget.value as WebhookAuth)}
          >
            {WEBHOOK_AUTHS.map((mode) => (
              <option key={mode} value={mode}>
                {AUTH_LABEL[mode]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Description" htmlFor={`${ids}-description`}>
          <input
            id={`${ids}-description`}
            className={inputClass}
            value={description}
            onChange={(e) => setDescription(e.currentTarget.value)}
          />
        </Field>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div>
          <Button type="submit" tone="primary" disabled={create.isPending}>
            Add route
          </Button>
        </div>
      </form>
    </Card>
  );
}

function RouteRow({ route, onDelete }: { route: WebhookRoute; onDelete: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.webhooks.setEnabled(route.name, enabled),
    onSuccess: (_reply, enabled) =>
      client.setQueryData<WebhooksResponse>(["webhooks"], (data) =>
        data
          ? {
              ...data,
              routes: data.routes.map((r) => (r.name === route.name ? { ...r, enabled } : r)),
            }
          : data,
      ),
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const fromConfig = route.source === "config";
  return (
    <li
      data-testid={`webhook-${route.name}`}
      className="flex flex-col gap-2 rounded-md border border-line bg-raised p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 text-sm font-medium">{route.name}</span>
        {fromConfig ? <Badge>config file</Badge> : null}
        <Badge tone={route.secret_set ? "ok" : "warn"}>
          {route.secret_set ? "secret set" : "no secret"}
        </Badge>
        <Switch
          checked={route.enabled}
          onCheckedChange={(enabled) => toggle.mutate(enabled)}
          label={`Enable ${route.name}`}
          disabled={fromConfig || toggle.isPending}
        />
      </div>
      <code className="font-mono text-xs break-all">{route.url}</code>
      <p className="text-xs text-muted">
        Signature: {route.auth} · Events: {route.events.length ? route.events.join(", ") : "all"}
        {" · "}
        Delivery: {route.deliver ?? "log"}
        {route.deliver_only ? " (direct)" : ""}
      </p>
      {route.description ? <p className="text-xs text-muted">{route.description}</p> : null}
      {fromConfig ? (
        <p className="text-xs text-muted">Change this route in config.yaml.</p>
      ) : (
        <div>
          <Button tone="danger" onClick={onDelete}>
            Delete
          </Button>
        </div>
      )}
    </li>
  );
}

export function WebhooksPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [created, setCreated] = useState<Created | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const routes = useQuery({ queryKey: ["webhooks"], queryFn: () => api.webhooks.list() });
  const receiver = useMutation({
    mutationFn: (enabled: boolean) => api.webhooks.setReceiver(enabled),
    onSuccess: (_reply, enabled) => {
      toast.show(
        enabled
          ? "Receiver switched on; it starts with the daemon's next start."
          : "Receiver switched off from the daemon's next start.",
        "success",
      );
      client.setQueryData<WebhooksResponse>(["webhooks"], (data) =>
        data ? { ...data, receiver_enabled: enabled } : data,
      );
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.webhooks.remove(name),
    onSuccess: (_reply, name) => {
      setDeleting(null);
      toast.show(`${name} deleted.`, "success");
      void client.invalidateQueries({ queryKey: ["webhooks"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const data = routes.data;
  return (
    <div className="flex flex-col gap-4 p-4">
      {data ? (
        <Card className="flex flex-wrap items-center gap-3">
          <div className="flex-1">
            <p className="text-sm font-medium">Webhook receiver</p>
            <p className="text-xs text-muted">
              Listens at <code className="font-mono">{data.base_url}</code>. Switching it takes
              effect at the daemon's next start; a route switched off below refuses requests at
              once.
            </p>
          </div>
          <Switch
            checked={data.receiver_enabled}
            onCheckedChange={(enabled) => receiver.mutate(enabled)}
            label="Webhook receiver on"
            disabled={receiver.isPending}
          />
        </Card>
      ) : null}
      {created ? <CreatedRoute created={created} onDone={() => setCreated(null)} /> : null}
      <QueryState
        loading={routes.isPending}
        error={routes.error}
        empty={(data?.routes.length ?? 0) === 0}
        emptyText="No webhook routes yet."
      >
        <ul className="flex flex-col gap-2" aria-label="Webhook routes">
          {(data?.routes ?? []).map((route) => (
            <RouteRow key={route.name} route={route} onDelete={() => setDeleting(route.name)} />
          ))}
        </ul>
      </QueryState>
      <CreateRoute onCreated={setCreated} />
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting ?? ""}?`}
        description="The route and its signing secret are removed; senders using it get refused."
        confirmLabel="Delete"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (deleting) remove.mutate(deleting);
        }}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}
