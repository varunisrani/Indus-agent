// Models (P18-24): the profile's main model and its context window, the provider inventory, the
// side-task (auxiliary) models, and provider sign-in by device code: Start shows the code and the
// page to enter it on, then the session is polled at the interval the provider asked for until it
// is approved, refused, expired or cancelled. No token ever reaches the browser.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { api } from "../lib/api.ts";
import type { AuxiliaryTask, LoginStarted, LoginStatus, ProviderOption } from "../lib/restTypes.ts";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../ui/controls.tsx";

/** Never poll faster than this, whatever the provider says. */
export const MIN_POLL_S = 1;

const LOGIN_TEXT: Record<LoginStatus, string> = {
  pending: "Waiting for you to approve the sign-in…",
  approved: "Signed in; the credential was added to this profile.",
  denied: "The sign-in was refused.",
  expired: "The code expired before it was approved. Start again.",
  failed: "The sign-in failed.",
  cancelled: "The sign-in was cancelled.",
};

function useModelsChanged() {
  const client = useQueryClient();
  return () => void client.invalidateQueries({ queryKey: ["models"] });
}

function DeviceLogin({ provider, onDone }: { provider: ProviderOption; onDone: () => void }) {
  const client = useQueryClient();
  const [grant, setGrant] = useState<LoginStarted | null>(null);
  const [status, setStatus] = useState<LoginStatus>("pending");
  const [message, setMessage] = useState("");
  const [problem, setProblem] = useState("");
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stop = () => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  };
  useEffect(
    () => () => {
      if (timer.current !== null) clearInterval(timer.current);
    },
    [],
  );

  const finish = (next: LoginStatus, text: string) => {
    stop();
    setStatus(next);
    setMessage(text);
    if (next === "approved") void client.invalidateQueries({ queryKey: ["models"] });
  };

  const start = useMutation({
    mutationFn: () => api.providerLogin.start(provider.name),
    onSuccess: (started) => {
      setGrant(started);
      setStatus("pending");
      setMessage("");
      setProblem("");
      stop();
      const every = Math.max(started.interval || MIN_POLL_S, MIN_POLL_S) * 1000;
      timer.current = setInterval(() => {
        api.providerLogin.poll(started.session_id).then(
          (reply) => {
            if (timer.current === null) return; // cancelled meanwhile
            if (reply.status !== "pending") finish(reply.status, reply.message);
          },
          () => {
            // A failed poll is not an answer: keep asking until the session says how it ended.
          },
        );
      }, every);
    },
    onError: (error) => setProblem(errorText(error)),
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.providerLogin.cancel(id),
    onMutate: () => stop(),
    onSettled: () => finish("cancelled", ""),
  });

  return (
    <section
      aria-label={`Sign in to ${provider.label}`}
      className="flex flex-col gap-2 rounded-md border border-line p-3"
    >
      {!grant ? (
        <div className="flex gap-2">
          <Button tone="primary" onClick={() => start.mutate()} disabled={start.isPending}>
            {start.isPending ? "Asking for a code…" : "Get a sign-in code"}
          </Button>
          <Button onClick={onDone}>Close</Button>
        </div>
      ) : (
        <>
          <p className="text-sm">
            Open{" "}
            <a
              href={grant.verification_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline"
            >
              {grant.verification_url}
            </a>{" "}
            and enter this code:
          </p>
          <code data-testid="device-code" className="font-mono text-lg tracking-widest">
            {grant.user_code}
          </code>
          <p role="status" className="text-sm text-muted">
            {message || LOGIN_TEXT[status]}
          </p>
          <div className="flex gap-2">
            {status === "pending" ? (
              <Button
                tone="danger"
                onClick={() => cancel.mutate(grant.session_id)}
                disabled={cancel.isPending}
              >
                Cancel
              </Button>
            ) : (
              <Button onClick={onDone}>Close</Button>
            )}
          </div>
        </>
      )}
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
    </section>
  );
}

function MainModel() {
  const toast = useToast();
  const changed = useModelsChanged();
  const ids = useId();
  const info = useQuery({ queryKey: ["models", "info"], queryFn: () => api.model.info() });
  const options = useQuery({ queryKey: ["models", "options"], queryFn: () => api.model.options() });
  const [provider, setProvider] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const shownProvider = provider ?? options.data?.current.provider ?? "";
  const shownModel = model ?? options.data?.current.model ?? "";
  const save = useMutation({
    mutationFn: () =>
      api.model.set({ scope: "main", provider: shownProvider, model: shownModel.trim() }),
    onSuccess: () => {
      toast.show("Main model saved; new sessions use it.", "success");
      setProvider(null);
      setModel(null);
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (shownModel.trim()) save.mutate();
  };
  const data = info.data;
  return (
    <Card className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Main model</h2>
      <QueryState loading={info.isPending} error={info.error}>
        <p className="text-sm" data-testid="current-model">
          {data?.model ?? "No model configured"}
          {data?.provider ? ` · ${data.provider}` : ""}
          {data?.effective_context_length
            ? ` · ${new Intl.NumberFormat("en").format(data.effective_context_length)} token context`
            : ""}
        </p>
      </QueryState>
      <form className="flex flex-wrap items-end gap-2" onSubmit={submit} noValidate>
        <Field label="Provider" htmlFor={`${ids}-provider`}>
          <select
            id={`${ids}-provider`}
            className={inputClass}
            value={shownProvider}
            onChange={(e) => setProvider(e.currentTarget.value)}
          >
            <option value="">(keep)</option>
            {(options.data?.providers ?? []).map((p) => (
              <option key={p.name} value={p.name}>
                {p.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Model id" htmlFor={`${ids}-model`}>
          <input
            id={`${ids}-model`}
            className={inputClass}
            value={shownModel}
            onChange={(e) => setModel(e.currentTarget.value)}
          />
        </Field>
        <Button type="submit" tone="primary" disabled={!shownModel.trim() || save.isPending}>
          Save model
        </Button>
      </form>
    </Card>
  );
}

function Providers() {
  const [signingIn, setSigningIn] = useState<string | null>(null);
  const options = useQuery({ queryKey: ["models", "options"], queryFn: () => api.model.options() });
  const list = options.data?.providers ?? [];
  return (
    <Card className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Providers</h2>
      <QueryState
        loading={options.isPending}
        error={options.error}
        empty={list.length === 0}
        emptyText="No providers are known to this install."
      >
        <ul aria-label="Providers" className="flex flex-col gap-2">
          {list.map((p) => (
            <li
              key={p.name}
              data-testid={`provider-${p.name}`}
              className="flex flex-col gap-2 border-b border-line pb-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex-1 text-sm">
                  <strong>{p.label}</strong> <span className="text-xs text-muted">{p.name}</span>
                </span>
                <Badge tone={p.configured ? "ok" : "neutral"}>
                  {p.configured ? "configured" : "no key"}
                </Badge>
                {p.device_login && signingIn !== p.name ? (
                  <Button onClick={() => setSigningIn(p.name)}>Sign in</Button>
                ) : null}
              </div>
              {p.description ? <p className="text-xs text-muted">{p.description}</p> : null}
              {signingIn === p.name ? (
                <DeviceLogin provider={p} onDone={() => setSigningIn(null)} />
              ) : null}
            </li>
          ))}
        </ul>
      </QueryState>
      <p className="text-xs text-muted">
        Providers without a sign-in take an API key: add it on the Keys page or in the credential
        pool on the System page.
      </p>
    </Card>
  );
}

function AuxRow({ task }: { task: AuxiliaryTask }) {
  const toast = useToast();
  const changed = useModelsChanged();
  const ids = useId();
  const [provider, setProvider] = useState(task.provider);
  const [model, setModel] = useState(task.model);
  const save = useMutation({
    mutationFn: () =>
      api.model.set({
        scope: "auxiliary",
        task: task.task,
        provider: provider.trim(),
        model: model.trim(),
      }),
    onSuccess: () => {
      toast.show(`${task.task} model saved.`, "success");
      changed();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  return (
    <li data-testid={`aux-${task.task}`} className="flex flex-wrap items-end gap-2">
      <div className="min-w-40 flex-1">
        <p className="text-sm font-medium">{task.task}</p>
        {task.description ? <p className="text-xs text-muted">{task.description}</p> : null}
      </div>
      <Field label="Provider" htmlFor={`${ids}-provider`}>
        <input
          id={`${ids}-provider`}
          className={inputClass}
          value={provider}
          onChange={(e) => setProvider(e.currentTarget.value)}
        />
      </Field>
      <Field label="Model" htmlFor={`${ids}-model`}>
        <input
          id={`${ids}-model`}
          className={inputClass}
          placeholder="(the main model)"
          value={model}
          onChange={(e) => setModel(e.currentTarget.value)}
        />
      </Field>
      <Button onClick={() => save.mutate()} disabled={save.isPending}>
        Save
      </Button>
    </li>
  );
}

function Auxiliary() {
  const aux = useQuery({ queryKey: ["models", "auxiliary"], queryFn: () => api.model.auxiliary() });
  const tasks = aux.data?.tasks ?? [];
  return (
    <Card className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Side tasks</h2>
      <p className="text-xs text-muted">
        Smaller jobs (summaries, titles, vision…) can run on their own model; "auto" uses the main
        provider.
      </p>
      <QueryState
        loading={aux.isPending}
        error={aux.error}
        empty={tasks.length === 0}
        emptyText="No side tasks."
      >
        <ul aria-label="Side tasks" className="flex flex-col gap-3">
          {tasks.map((task) => (
            <AuxRow key={task.task} task={task} />
          ))}
        </ul>
      </QueryState>
    </Card>
  );
}

export function ModelsPage() {
  return (
    <div className="flex flex-col gap-4 p-4">
      <MainModel />
      <Providers />
      <Auxiliary />
    </div>
  );
}
