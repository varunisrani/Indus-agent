// Webhooks (P18-23): the gateway's webhook receiver for this profile. Routes added here get a fresh
// signing secret that the create answer carries once; this page keeps it only in the create
// dialog's state, so it is gone once the dialog closes or the page goes, and no list answer ever
// has it. Routes written in config.yaml are listed read-only.
// Look (W52-d): receiver card on top (warning-bordered while off), a headed list of route cards,
// and a "New route" dialog in the header that turns into the one-time secret view.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RotateCw, Trash2, TriangleAlert, Webhook } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import {
  WEBHOOK_AUTHS,
  type WebhookAuth,
  type WebhookCreate,
  type WebhookRoute,
  type WebhooksResponse,
} from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { ActionLog, useActionStarter } from "../ui/ActionLog.tsx";
import { CopyButton } from "../ui/CopyButton.tsx";
import {
  Badge,
  Button,
  Callout,
  Checkbox,
  CodeChip,
  EmptyCard,
  Field,
  IconButton,
  Input,
  PageSpinner,
  QueryState,
  SectionHeading,
  Select,
  Textarea,
} from "../ui/controls.tsx";
import { ConfirmDialog, DialogActions, Modal } from "../ui/Dialog.tsx";
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
    <section aria-label="New webhook route" className="flex flex-col gap-4">
      <p className="m-0 text-[0.8125rem] text-ink-muted">
        Route <strong className="text-ink">{created.name}</strong> created. Point the sender at its
        URL and give it the signing secret.
      </p>
      <div className="flex flex-col gap-1.5">
        <span className="type-label text-ink-muted">Webhook URL</span>
        <div className="flex items-center gap-2 border border-line px-3 py-2">
          <code className="min-w-0 flex-1 font-mono text-[0.75rem] break-all text-ink">
            {created.url}
          </code>
          <CopyButton value={created.url} label="Copy the route URL" look="icon" />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="type-label text-warning">Signing secret (shown once)</span>
        <div className="flex items-center gap-2 border border-warning/60 bg-warning-wash px-3 py-2">
          <code
            data-testid="webhook-secret"
            className="min-w-0 flex-1 font-mono text-[0.75rem] break-all text-ink"
          >
            {created.secret}
          </code>
          <CopyButton value={created.secret} label="Copy the signing secret" look="icon" />
        </div>
        <p className="m-0 text-[0.75rem] text-ink-soft">
          Copy the secret now and give it to the sender. You will not see it again: the dashboard
          keeps only its name.
        </p>
      </div>
      <DialogActions>
        <Button tone="primary" size="lg" onClick={onDone}>
          I have copied it
        </Button>
      </DialogActions>
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
    <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
      <Field label="Name" htmlFor={`${ids}-name`} hint="Part of the URL: /webhooks/<name>.">
        <Input
          id={`${ids}-name`}
          placeholder="github-push"
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
        />
      </Field>
      <Field label="Description" htmlFor={`${ids}-description`}>
        <Input
          id={`${ids}-description`}
          value={description}
          onChange={(e) => setDescription(e.currentTarget.value)}
        />
      </Field>
      <Field
        label="Events"
        htmlFor={`${ids}-events`}
        hint="Comma-separated; leave empty to accept every event."
      >
        <Input
          id={`${ids}-events`}
          placeholder="push, pull_request"
          value={events}
          onChange={(e) => setEvents(e.currentTarget.value)}
        />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Deliver to"
          htmlFor={`${ids}-deliver`}
          hint="A target such as telegram:<chat id>; empty keeps the answer in the log."
        >
          <Input
            id={`${ids}-deliver`}
            value={deliver}
            onChange={(e) => setDeliver(e.currentTarget.value)}
          />
        </Field>
        <div className="flex flex-col gap-1.5">
          <span className="type-label text-ink-muted">Deliver only</span>
          <label
            htmlFor={`${ids}-deliver-only`}
            className="flex min-h-8 items-center gap-2 text-[0.8125rem] text-ink"
          >
            <Checkbox
              id={`${ids}-deliver-only`}
              checked={deliverOnly}
              onChange={(e) => setDeliverOnly(e.currentTarget.checked)}
            />
            Pass the payload on without the agent
          </label>
        </div>
      </div>
      <Field label="Signature check" htmlFor={`${ids}-auth`}>
        <Select
          id={`${ids}-auth`}
          value={auth}
          onChange={(e) => setAuth(e.currentTarget.value as WebhookAuth)}
        >
          {WEBHOOK_AUTHS.map((mode) => (
            <option key={mode} value={mode}>
              {AUTH_LABEL[mode]}
            </option>
          ))}
        </Select>
      </Field>
      <Field
        label="Prompt"
        htmlFor={`${ids}-prompt`}
        hint="What the agent is asked; the payload is added."
      >
        <Textarea
          id={`${ids}-prompt`}
          className="min-h-20"
          rows={3}
          value={prompt}
          onChange={(e) => setPrompt(e.currentTarget.value)}
        />
      </Field>
      {problem ? (
        <p role="alert" className="m-0 text-[0.75rem] text-danger">
          {problem}
        </p>
      ) : null}
      <DialogActions>
        <Button type="submit" tone="primary" size="lg" disabled={create.isPending}>
          Add route
        </Button>
      </DialogActions>
    </form>
  );
}

function NewRouteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  // The secret lives here only; closing the dialog (or leaving the page) forgets it.
  const [created, setCreated] = useState<Created | null>(null);
  const close = () => {
    setCreated(null);
    onClose();
  };
  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
      }}
      title="New route"
      description={
        created
          ? "Copy what the sender needs before you close this."
          : "A URL that turns incoming events into a turn for the agent."
      }
    >
      {created ? (
        <CreatedRoute created={created} onDone={close} />
      ) : (
        <CreateRoute onCreated={setCreated} />
      )}
    </Modal>
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
      className="flex items-start gap-4 border border-line p-4"
    >
      <div className={`flex min-w-0 flex-1 flex-col gap-1.5 ${route.enabled ? "" : "opacity-60"}`}>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.875rem] font-medium text-ink">{route.name}</span>
          <Badge>{route.deliver ?? "log"}</Badge>
          {route.deliver_only ? <Badge tone="secondary">direct</Badge> : null}
          {route.enabled ? null : <Badge tone="warn">off</Badge>}
          {fromConfig ? <Badge tone="secondary">config file</Badge> : null}
          <Badge tone={route.secret_set ? "ok" : "warn"}>
            {route.secret_set ? "secret set" : "no secret"}
          </Badge>
        </div>
        {route.description ? (
          <p className="m-0 text-[0.75rem] text-ink-muted">{route.description}</p>
        ) : null}
        <div className="flex flex-wrap gap-1.5">
          {(route.events.length ? route.events : ["all"]).map((event) => (
            <Badge key={event} tone="secondary">
              {event}
            </Badge>
          ))}
        </div>
        <p className="m-0 text-[0.75rem] text-ink-muted">
          Signature: {route.auth} · Events: {route.events.length ? route.events.join(", ") : "all"}
          {" · "}
          Delivery: {route.deliver ?? "log"}
          {route.deliver_only ? " (direct)" : ""}
        </p>
        <div className="flex min-w-0 items-center gap-1">
          <code className="min-w-0 truncate font-mono text-[0.75rem] text-ink-muted">
            {route.url}
          </code>
          <CopyButton value={route.url} label="Copy the route URL" look="icon" />
        </div>
        {fromConfig ? (
          <p className="m-0 text-[0.75rem] text-ink-soft">Change this route in config.yaml.</p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Switch
          checked={route.enabled}
          onCheckedChange={(enabled) => toggle.mutate(enabled)}
          label={`Enable ${route.name}`}
          disabled={fromConfig || toggle.isPending}
        />
        {fromConfig ? null : (
          <IconButton label="Delete" tone="danger" onClick={onDelete}>
            <Trash2 />
          </IconButton>
        )}
      </div>
    </li>
  );
}

function Receiver({ data, onEnabled }: { data: WebhooksResponse; onEnabled: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const receiver = useMutation({
    mutationFn: (enabled: boolean) => api.webhooks.setReceiver(enabled),
    onSuccess: (_reply, enabled) => {
      toast.show(
        enabled
          ? "Receiver switched on; it starts with the daemon's next start."
          : "Receiver switched off from the daemon's next start.",
        "success",
      );
      client.setQueryData<WebhooksResponse>(["webhooks"], (all) =>
        all ? { ...all, receiver_enabled: enabled } : all,
      );
      if (enabled) onEnabled();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  if (!data.receiver_enabled) {
    return (
      <Callout
        tone="warning"
        icon={Webhook}
        title="Webhook receiver off"
        action={
          <Button
            tone="primary"
            size="sm"
            className="uppercase"
            icon={<Webhook />}
            disabled={receiver.isPending}
            onClick={() => receiver.mutate(true)}
          >
            Enable webhooks
          </Button>
        }
      >
        Switch it on to accept events over HTTP at <CodeChip>{data.base_url}</CodeChip>. It starts
        with the daemon; a chat channel is needed only when a route delivers its answer there.
      </Callout>
    );
  }
  return (
    <Callout
      icon={Webhook}
      action={
        <Switch
          checked={data.receiver_enabled}
          onCheckedChange={(enabled) => receiver.mutate(enabled)}
          label="Webhook receiver on"
          disabled={receiver.isPending}
        />
      }
    >
      <p className="m-0 font-medium">Webhook receiver</p>
      <p className="m-0 mt-1 text-[0.75rem] text-ink-muted">
        Listening at <CodeChip>{data.base_url}</CodeChip> while the daemon runs. Switching it off
        takes effect at the daemon's next start.
      </p>
    </Callout>
  );
}

export function WebhooksPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [restartNote, setRestartNote] = useState(false);
  const restart = useActionStarter(() => api.daemon.restart());
  const routes = useQuery({ queryKey: ["webhooks"], queryFn: () => api.webhooks.list() });
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
  const list = data?.routes ?? [];
  return (
    <div className="flex flex-col gap-6 px-4 py-[1.375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderActions>
        <Button
          tone="primary"
          size="sm"
          className="uppercase"
          icon={<Plus />}
          disabled={!data?.receiver_enabled}
          title={data?.receiver_enabled ? undefined : "Switch the receiver on first"}
          onClick={() => setCreating(true)}
        >
          New route
        </Button>
      </HeaderActions>
      {data ? <Receiver data={data} onEnabled={() => setRestartNote(true)} /> : null}
      {restartNote ? (
        <Callout
          tone="warning"
          icon={TriangleAlert}
          action={
            <Button
              tone="primary"
              size="sm"
              className="uppercase"
              icon={<RotateCw />}
              disabled={restart.pending}
              onClick={() => restart.start()}
            >
              Restart daemon
            </Button>
          }
        >
          Receiver on; it starts with the daemon's next start.
        </Callout>
      ) : null}
      {restart.run > 0 ? (
        <ActionLog
          name="daemon-restart"
          run={restart.run}
          title="Daemon restart"
          onFinished={() => setRestartNote(false)}
        />
      ) : null}
      {routes.isPending ? (
        <PageSpinner />
      ) : (
        <QueryState loading={false} error={routes.error}>
          <section aria-label="Routes" className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <SectionHeading icon={Webhook}>Routes ({list.length})</SectionHeading>
              <p className="m-0 text-[0.75rem] text-ink-muted">
                Route changes apply at once while the receiver runs; a route switched off refuses
                requests.
              </p>
            </div>
            {list.length === 0 ? (
              <EmptyCard>No webhook routes yet.</EmptyCard>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Webhook routes">
                {list.map((route) => (
                  <RouteRow
                    key={route.name}
                    route={route}
                    onDelete={() => setDeleting(route.name)}
                  />
                ))}
              </ul>
            )}
          </section>
        </QueryState>
      )}
      <NewRouteDialog open={creating} onClose={() => setCreating(false)} />
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
