// System page: maintenance operations (doctor, backup, import, checkpoints) and shell hooks. Every
// long operation is a background action on the server; its output is tailed below its buttons.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import { formatBytes, relativeTime } from "../../lib/format.ts";
import type { ActionStarted, ShellHook, ShellHookCreate } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { ActionLog, useActionStarter } from "../../ui/ActionLog.tsx";
import { Badge, Button, Card, Field, inputClass, Notice, QueryState } from "../../ui/controls.tsx";
import { ConfirmDialog } from "../../ui/Dialog.tsx";

type Operation = "doctor" | "backup" | "import";

interface ImportArgs {
  archive: string;
  file: File | null;
  force: boolean;
}

export function OperationsSection() {
  const toast = useToast();
  const ids = useId();
  const [current, setCurrent] = useState<Operation>("doctor");
  const [backupReady, setBackupReady] = useState<string | null>(null);
  const [archive, setArchive] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [force, setForce] = useState(false);
  const [problem, setProblem] = useState("");

  const action = useActionStarter(
    (args: { op: Operation; input?: ImportArgs }): Promise<ActionStarted> => {
      if (args.op === "doctor") return api.system.doctor();
      if (args.op === "backup") return api.system.backup();
      const input = args.input as ImportArgs;
      return input.file
        ? api.system.importUpload(input.file, input.force)
        : api.system.importArchive(input.archive, input.force);
    },
  );
  const start = (op: Operation, input?: ImportArgs) => {
    setCurrent(op);
    setBackupReady(null);
    action.start(input ? { op, input } : { op });
  };

  const submitImport = (event: FormEvent) => {
    event.preventDefault();
    if (!file && !archive.trim().startsWith("/")) {
      setProblem("Choose a zip file, or give an archive's full path on this machine.");
      return;
    }
    setProblem("");
    start("import", { archive: archive.trim(), file, force });
  };

  const download = useMutation({
    mutationFn: (path: string) => api.system.downloadBackup(path),
    onError: (error) => toast.show(errorText(error), "error"),
  });

  return (
    <Card className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Operations</h2>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => start("doctor")} disabled={action.pending}>
          Run doctor
        </Button>
        <Button onClick={() => start("backup")} disabled={action.pending}>
          Back up this profile
        </Button>
      </div>
      <form className="flex flex-wrap items-end gap-2" onSubmit={submitImport} noValidate>
        <Field label="Import archive path" htmlFor={`${ids}-archive`}>
          <input
            id={`${ids}-archive`}
            className={`${inputClass} font-mono`}
            value={archive}
            onChange={(e) => setArchive(e.currentTarget.value)}
          />
        </Field>
        <Field label="…or upload a zip" htmlFor={`${ids}-file`}>
          <input
            id={`${ids}-file`}
            type="file"
            accept=".zip,application/zip"
            className="text-sm"
            onChange={(e) => setFile(e.currentTarget.files?.[0] ?? null)}
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={force}
            onChange={(e) => setForce(e.currentTarget.checked)}
          />
          Overwrite existing files
        </label>
        <Button type="submit" disabled={action.pending}>
          Import
        </Button>
      </form>
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
      {action.run > 0 ? (
        <ActionLog
          name={current}
          run={action.run}
          title={current === "doctor" ? "Doctor" : current === "backup" ? "Backup" : "Import"}
          onFinished={(status) => {
            if (current === "backup" && status.state === "done" && action.started?.archive) {
              setBackupReady(action.started.archive);
            }
          }}
        />
      ) : null}
      {backupReady ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="flex-1">
            Backup written: <code className="font-mono text-xs">{backupReady}</code>
          </span>
          <Button onClick={() => download.mutate(backupReady)} disabled={download.isPending}>
            Download
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

export function CheckpointsSection() {
  const client = useQueryClient();
  const ids = useId();
  const [days, setDays] = useState("7");
  const [size, setSize] = useState("500");
  const checkpoints = useQuery({
    queryKey: ["system", "checkpoints"],
    queryFn: () => api.system.checkpoints(),
  });
  const prune = useActionStarter((rules: { days: number; size: number }) =>
    api.system.pruneCheckpoints(rules.days, rules.size),
  );
  const daysValue = Number(days);
  const sizeValue = Number(size);
  const valid =
    Number.isInteger(daysValue) && daysValue >= 0 && Number.isInteger(sizeValue) && sizeValue >= 0;
  const projects = checkpoints.data?.projects ?? [];
  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="flex-1 text-sm font-semibold">Checkpoints</h2>
        {checkpoints.data ? (
          <Badge>{formatBytes(checkpoints.data.total_bytes) || "0 B"}</Badge>
        ) : null}
      </div>
      <QueryState
        loading={checkpoints.isPending}
        error={checkpoints.error}
        empty={projects.length === 0}
        emptyText="No file checkpoints kept."
      >
        <ul aria-label="Checkpoint projects" className="flex flex-col gap-1 text-sm">
          {projects.map((p) => (
            <li key={p.key} className="flex flex-wrap gap-2">
              <code className="flex-1 font-mono text-xs break-all">{p.workdir}</code>
              <span className="text-xs text-muted">
                {p.commits} snapshot{p.commits === 1 ? "" : "s"} · {formatBytes(p.bytes)} ·{" "}
                {relativeTime(p.last_touch)}
                {p.exists ? "" : " · folder gone"}
              </span>
            </li>
          ))}
        </ul>
      </QueryState>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Keep days" htmlFor={`${ids}-days`}>
          <input
            id={`${ids}-days`}
            className={inputClass}
            inputMode="numeric"
            value={days}
            onChange={(e) => setDays(e.currentTarget.value)}
          />
        </Field>
        <Field label="Size cap (MB)" htmlFor={`${ids}-size`}>
          <input
            id={`${ids}-size`}
            className={inputClass}
            inputMode="numeric"
            value={size}
            onChange={(e) => setSize(e.currentTarget.value)}
          />
        </Field>
        <Button
          onClick={() => prune.start({ days: daysValue, size: sizeValue })}
          disabled={!valid || prune.pending}
        >
          Prune checkpoints
        </Button>
      </div>
      {prune.run > 0 ? (
        <ActionLog
          name="checkpoints-prune"
          run={prune.run}
          title="Checkpoint prune"
          onFinished={() => void client.invalidateQueries({ queryKey: ["system", "checkpoints"] })}
        />
      ) : null}
    </Card>
  );
}

function AddHook({ events }: { events: string[] }) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [event, setEvent] = useState(events[0] ?? "");
  const [command, setCommand] = useState("");
  const [matcher, setMatcher] = useState("");
  const [timeout, setTimeoutValue] = useState("");
  const [consent, setConsent] = useState(false);
  const [problem, setProblem] = useState("");
  const add = useMutation({
    mutationFn: (body: ShellHookCreate) => api.system.addHook(body),
    onSuccess: () => {
      setCommand("");
      setMatcher("");
      setTimeoutValue("");
      setConsent(false);
      setProblem("");
      toast.show("Hook added and approved.", "success");
      void client.invalidateQueries({ queryKey: ["system", "hooks"] });
    },
    onError: (error) => setProblem(errorText(error)),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!consent) return;
    if (!command.trim()) {
      setProblem("Name the command to run.");
      return;
    }
    const body: ShellHookCreate = { event, command: command.trim(), consent: true };
    if (matcher.trim()) body.matcher = matcher.trim();
    const seconds = Number(timeout);
    if (timeout.trim() && Number.isFinite(seconds) && seconds > 0) body.timeout = seconds;
    add.mutate(body);
  };
  return (
    <form className="flex flex-col gap-2" onSubmit={submit} noValidate>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Event" htmlFor={`${ids}-event`}>
          <select
            id={`${ids}-event`}
            className={inputClass}
            value={event}
            onChange={(e) => setEvent(e.currentTarget.value)}
          >
            {events.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Command" htmlFor={`${ids}-command`}>
          <input
            id={`${ids}-command`}
            className={`${inputClass} font-mono`}
            value={command}
            onChange={(e) => setCommand(e.currentTarget.value)}
          />
        </Field>
        <Field label="Tool matcher (optional)" htmlFor={`${ids}-matcher`}>
          <input
            id={`${ids}-matcher`}
            className={inputClass}
            value={matcher}
            onChange={(e) => setMatcher(e.currentTarget.value)}
          />
        </Field>
        <Field label="Timeout s (optional)" htmlFor={`${ids}-timeout`}>
          <input
            id={`${ids}-timeout`}
            className={inputClass}
            inputMode="decimal"
            value={timeout}
            onChange={(e) => setTimeoutValue(e.currentTarget.value)}
          />
        </Field>
      </div>
      <Notice tone="warn">
        A shell hook runs this command on your machine, as you, every time the event happens, with
        the event's details as input. Only add commands you wrote or trust.
      </Notice>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => setConsent(e.currentTarget.checked)}
        />
        I understand and want this command to run on agent events
      </label>
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
      <div>
        <Button type="submit" tone="primary" disabled={!consent || add.isPending}>
          Add hook
        </Button>
      </div>
    </form>
  );
}

export function HooksSection() {
  const toast = useToast();
  const client = useQueryClient();
  const [removing, setRemoving] = useState<ShellHook | null>(null);
  const hooks = useQuery({ queryKey: ["system", "hooks"], queryFn: () => api.system.hooks() });
  const remove = useMutation({
    mutationFn: (hook: ShellHook) => api.system.removeHook(hook.event, hook.command),
    onSuccess: () => {
      setRemoving(null);
      toast.show("Hook removed.", "success");
      void client.invalidateQueries({ queryKey: ["system", "hooks"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const list = hooks.data?.hooks ?? [];
  return (
    <Card className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold">Shell hooks</h2>
      <QueryState loading={hooks.isPending} error={hooks.error}>
        {hooks.data?.warnings.length ? (
          <Notice tone="warn">{hooks.data.warnings.join(" ")}</Notice>
        ) : null}
        {list.length === 0 ? (
          <p className="text-sm text-muted">No shell hooks.</p>
        ) : (
          <ul aria-label="Shell hooks" className="flex flex-col gap-1">
            {list.map((hook) => (
              <li
                key={`${hook.event}-${hook.command}`}
                data-testid={`hook-${hook.grant_id}`}
                className="flex flex-wrap items-center gap-2 text-sm"
              >
                <Badge>{hook.event}</Badge>
                <code className="flex-1 font-mono text-xs break-all">{hook.command}</code>
                {hook.matcher ? <span className="text-xs text-muted">{hook.matcher}</span> : null}
                <Badge tone={hook.approved ? "ok" : "warn"}>
                  {hook.approved ? "approved" : "not approved"}
                </Badge>
                {hook.executable ? null : <Badge tone="warn">not executable</Badge>}
                <Button tone="danger" onClick={() => setRemoving(hook)}>
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        )}
        <AddHook events={hooks.data?.events ?? []} />
      </QueryState>
      <ConfirmDialog
        open={removing !== null}
        title="Remove this hook?"
        description="The command no longer runs on agent events, and its approval is dropped."
        confirmLabel="Remove"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (removing) remove.mutate(removing);
        }}
        onCancel={() => setRemoving(null)}
      />
    </Card>
  );
}
