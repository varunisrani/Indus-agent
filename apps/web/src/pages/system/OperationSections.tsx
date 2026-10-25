// System page: Operations (console, doctor, backup and restore), Checkpoints and Shell hooks.
// Every long operation is a background action on the server; its output goes to the page's
// action log at the top.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Database,
  Download,
  LoaderCircle,
  Plus,
  SquareTerminal,
  Stethoscope,
  Trash2,
  Upload,
} from "lucide-react";
import { type FormEvent, useId, useRef, useState } from "react";
import { ConsoleModal } from "../../console/ConsoleModal.tsx";
import { api } from "../../lib/api.ts";
import { consoleEnabled } from "../../lib/bootstrap.ts";
import { formatSize, relativeTime } from "../../lib/format.ts";
import type { ShellHook, ShellHookCreate } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Field,
  IconButton,
  Input,
  inputClass,
  Notice,
  QueryState,
  Select,
} from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { ConfirmDialog, DialogActions, Modal } from "../../ui/Dialog.tsx";
import { SystemSection, tileLabel, useLoggedAction } from "./frame.tsx";

type Operation = "doctor" | "backup" | "import";

interface ImportArgs {
  archive: string;
  file: File | null;
  force: boolean;
}

const OP_TITLE: Record<Operation, string> = {
  doctor: "doctor",
  backup: "backup",
  import: "import",
};

const rowClass = "flex flex-col gap-2 border-t border-line pt-4 first:border-t-0 first:pt-0";

export function OperationsSection() {
  const toast = useToast();
  const ids = useId();
  const picker = useRef<HTMLInputElement>(null);
  const [console_, setConsole] = useState(false);
  const [backupReady, setBackupReady] = useState<string | null>(null);
  const [archive, setArchive] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [force, setForce] = useState(false);
  const [problem, setProblem] = useState("");
  const [restoring, setRestoring] = useState<ImportArgs | null>(null);

  const action = useLoggedAction(
    (args: { op: Operation; input?: ImportArgs }) => {
      if (args.op === "doctor") return api.system.doctor();
      if (args.op === "backup") return api.system.backup();
      const input = args.input as ImportArgs;
      return input.file
        ? api.system.importUpload(input.file, input.force)
        : api.system.importArchive(input.archive, input.force);
    },
    (args) => ({ name: args.op, title: OP_TITLE[args.op] }),
    (status, started) => {
      if (status.name === "backup" && status.state === "done" && started?.archive) {
        setBackupReady(started.archive);
      }
    },
  );
  const start = (op: Operation, input?: ImportArgs) => {
    if (op === "backup") setBackupReady(null);
    action.start(input ? { op, input } : { op });
  };

  const askPath = (event: FormEvent) => {
    event.preventDefault();
    if (!archive.trim().startsWith("/")) {
      setProblem("Choose a zip file, or give an archive's full path on this machine.");
      return;
    }
    setProblem("");
    setRestoring({ archive: archive.trim(), file: null, force });
  };

  const download = useMutation({
    mutationFn: (path: string) => api.system.downloadBackup(path),
    onError: (error) => toast.show(errorText(error), "error"),
  });

  return (
    <SystemSection icon={Activity} title="Operations">
      <Card className="flex flex-wrap gap-2">
        {consoleEnabled() ? (
          <Button tone="ghost" size="sm" icon={<SquareTerminal />} onClick={() => setConsole(true)}>
            Open console
          </Button>
        ) : null}
        <Button
          tone="ghost"
          size="sm"
          icon={<Stethoscope />}
          onClick={() => start("doctor")}
          disabled={action.pending}
        >
          Run doctor
        </Button>
      </Card>
      <Card className="flex flex-col gap-4">
        <div className={rowClass}>
          <p className={cx("m-0", tileLabel)}>Full backup</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              tone="ghost"
              size="sm"
              icon={<Database />}
              onClick={() => start("backup")}
              disabled={action.pending}
            >
              Create backup
            </Button>
            <Button
              tone="ghost"
              size="sm"
              icon={<Download />}
              onClick={() => backupReady && download.mutate(backupReady)}
              disabled={!backupReady || download.isPending}
            >
              Download backup
            </Button>
            <span className="min-w-0 flex-1 truncate font-mono text-[0.75rem] text-ink-muted">
              {backupReady ?? "No backup created yet"}
            </span>
          </div>
        </div>
        <div className={rowClass}>
          <p className={cx("m-0", tileLabel)}>Restore from upload</p>
          {/* relative: the hidden picker is placed in this row, not below the whole page */}
          <div className="relative flex flex-wrap items-center gap-2">
            <input
              ref={picker}
              type="file"
              accept=".zip,application/zip"
              aria-label="Restore zip file"
              className="sr-only"
              tabIndex={-1}
              onChange={(e) => setFile(e.currentTarget.files?.[0] ?? null)}
            />
            <Button
              tone="ghost"
              size="sm"
              icon={<Upload />}
              onClick={() => picker.current?.click()}
            >
              Choose restore zip
            </Button>
            <span className="min-w-0 flex-1 truncate text-[0.75rem] text-ink-muted">
              {file ? file.name : "No archive selected"}
            </span>
            <Button
              tone="ghost"
              size="sm"
              onClick={() => file && setRestoring({ archive: "", file, force })}
              disabled={!file || action.pending}
            >
              Restore upload
            </Button>
          </div>
        </div>
        <form className={rowClass} onSubmit={askPath} noValidate>
          <label htmlFor={`${ids}-archive`} className={tileLabel}>
            Restore from path
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <input
              id={`${ids}-archive`}
              className={cx(inputClass, "h-[2.125rem] min-w-0 flex-1")}
              placeholder="/home/me/.indus/backups/backup.zip"
              value={archive}
              onChange={(e) => setArchive(e.currentTarget.value)}
            />
            <Button
              type="submit"
              tone="ghost"
              size="sm"
              disabled={!archive.trim() || action.pending}
            >
              Restore path
            </Button>
          </div>
          {problem ? <Notice tone="danger">{problem}</Notice> : null}
        </form>
        <div className="flex items-center gap-2 border-t border-line pt-4">
          <Checkbox
            id={`${ids}-force`}
            checked={force}
            onChange={(e) => setForce(e.currentTarget.checked)}
          />
          <label htmlFor={`${ids}-force`} className="text-[0.75rem] text-ink-muted">
            Overwrite existing files
          </label>
        </div>
      </Card>
      <ConfirmDialog
        open={restoring !== null}
        title="Restore this backup?"
        description={
          restoring?.force
            ? "Files in this profile are replaced by the archive's copies where both exist."
            : "The archive's files are added to this profile; files already here are kept."
        }
        confirmLabel="Restore"
        danger
        onConfirm={() => {
          if (restoring) start("import", restoring);
          setRestoring(null);
        }}
        onCancel={() => setRestoring(null)}
      />
      {console_ ? <ConsoleModal open onClose={() => setConsole(false)} /> : null}
    </SystemSection>
  );
}

export function CheckpointsSection() {
  const client = useQueryClient();
  const ids = useId();
  const [days, setDays] = useState("7");
  const [size, setSize] = useState("500");
  const [asking, setAsking] = useState(false);
  const checkpoints = useQuery({
    queryKey: ["system", "checkpoints"],
    queryFn: () => api.system.checkpoints(),
  });
  const prune = useLoggedAction(
    (rules: { days: number; size: number }) => api.system.pruneCheckpoints(rules.days, rules.size),
    () => ({ name: "checkpoints-prune", title: "checkpoints prune" }),
    () => void client.invalidateQueries({ queryKey: ["system", "checkpoints"] }),
  );
  const daysValue = Number(days);
  const sizeValue = Number(size);
  const valid =
    days.trim() !== "" &&
    size.trim() !== "" &&
    Number.isInteger(daysValue) &&
    daysValue >= 0 &&
    Number.isInteger(sizeValue) &&
    sizeValue >= 0;
  const projects = checkpoints.data?.projects ?? [];
  return (
    <SystemSection icon={Database} title="Checkpoints">
      <Card className="flex flex-col gap-3">
        <QueryState
          loading={checkpoints.isPending}
          error={checkpoints.error}
          what="the checkpoints"
          onRetry={() => void checkpoints.refetch()}
        >
          <div className="flex flex-wrap items-end gap-3">
            <p className="m-0 flex-1 self-center text-[0.875rem] text-ink-muted">
              {projects.length} project{projects.length === 1 ? "" : "s"} ·{" "}
              {formatSize(checkpoints.data?.total_bytes ?? 0)}
            </p>
            <div className="w-24">
              <Field label="Keep days" htmlFor={`${ids}-days`}>
                <Input
                  id={`${ids}-days`}
                  inputMode="numeric"
                  value={days}
                  onChange={(e) => setDays(e.currentTarget.value)}
                />
              </Field>
            </div>
            <div className="w-24">
              <Field label="Size cap (MB)" htmlFor={`${ids}-size`}>
                <Input
                  id={`${ids}-size`}
                  inputMode="numeric"
                  value={size}
                  onChange={(e) => setSize(e.currentTarget.value)}
                />
              </Field>
            </div>
            <Button
              tone="ghost"
              size="sm"
              className="text-danger"
              icon={<Trash2 />}
              aria-label="Prune checkpoints"
              onClick={() => setAsking(true)}
              disabled={!valid || prune.pending || projects.length === 0}
            >
              Prune
            </Button>
          </div>
          {projects.length > 0 ? (
            <ul
              aria-label="Checkpoint projects"
              className="m-0 flex list-none flex-col gap-1 border-t border-line p-0 pt-3"
            >
              {projects.map((p) => (
                <li key={p.key} className="flex flex-wrap gap-2 text-[0.75rem]">
                  <code className="min-w-0 flex-1 font-mono break-all text-ink">{p.workdir}</code>
                  <span className="text-ink-muted">
                    {p.commits} snapshot{p.commits === 1 ? "" : "s"} · {formatSize(p.bytes)} ·{" "}
                    {relativeTime(p.last_touch)}
                    {p.exists ? "" : " · folder gone"}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </QueryState>
      </Card>
      <ConfirmDialog
        open={asking}
        title="Prune checkpoints?"
        description={`Snapshots older than ${days} day(s) are dropped, then the oldest go until the store is under ${size} MB.`}
        confirmLabel="Prune"
        danger
        onConfirm={() => {
          setAsking(false);
          prune.start({ days: daysValue, size: sizeValue });
        }}
        onCancel={() => setAsking(false)}
      />
    </SystemSection>
  );
}

function HookDialog({ events, onClose }: { events: string[]; onClose: () => void }) {
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
      toast.show("Hook added and approved.", "success");
      void client.invalidateQueries({ queryKey: ["system", "hooks"] });
      onClose();
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
    <Modal
      open
      onOpenChange={(next) => (next ? undefined : onClose())}
      title="New shell hook"
      description="A command of yours that runs when an agent event happens."
    >
      <form className="flex flex-col gap-4 py-1" onSubmit={submit} noValidate>
        <Field label="Event" htmlFor={`${ids}-event`}>
          <Select
            id={`${ids}-event`}
            value={event}
            onChange={(e) => setEvent(e.currentTarget.value)}
          >
            {events.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Command" htmlFor={`${ids}-command`}>
          <Input
            id={`${ids}-command`}
            autoFocus
            placeholder="~/hooks/log.sh"
            value={command}
            onChange={(e) => setCommand(e.currentTarget.value)}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Tool matcher (optional)" htmlFor={`${ids}-matcher`}>
            <Input
              id={`${ids}-matcher`}
              value={matcher}
              onChange={(e) => setMatcher(e.currentTarget.value)}
            />
          </Field>
          <Field label="Timeout (s)" htmlFor={`${ids}-timeout`}>
            <Input
              id={`${ids}-timeout`}
              inputMode="decimal"
              value={timeout}
              onChange={(e) => setTimeoutValue(e.currentTarget.value)}
            />
          </Field>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id={`${ids}-consent`}
            checked={consent}
            onChange={(e) => setConsent(e.currentTarget.checked)}
          />
          <label htmlFor={`${ids}-consent`} className="text-[0.8125rem] text-ink">
            I understand and want this command to run on agent events
          </label>
        </div>
        <p className="m-0 text-[0.75rem] text-warning">
          A shell hook runs this command on your machine, as you, every time the event happens, with
          the event's details as input. Only add commands you wrote or trust.
        </p>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <DialogActions>
          <Button
            type="submit"
            tone="primary"
            size="sm"
            className="uppercase"
            icon={add.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}
            disabled={!consent || add.isPending}
          >
            Add hook
          </Button>
        </DialogActions>
      </form>
    </Modal>
  );
}

export function HooksSection() {
  const toast = useToast();
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);
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
    <SystemSection
      icon={SquareTerminal}
      title="Shell hooks"
      aside={
        <Button
          tone="primary"
          size="sm"
          className="uppercase"
          icon={<Plus />}
          onClick={() => setAdding(true)}
          disabled={!hooks.data}
        >
          New hook
        </Button>
      }
    >
      <QueryState
        loading={hooks.isPending}
        error={hooks.error}
        what="the shell hooks"
        onRetry={() => void hooks.refetch()}
      >
        {hooks.data?.warnings.length ? (
          <Notice tone="warn">{hooks.data.warnings.join(" ")}</Notice>
        ) : null}
        {list.length === 0 ? (
          <Card className="py-6 text-center text-[0.875rem] text-ink-muted">No shell hooks.</Card>
        ) : (
          <ul aria-label="Shell hooks" className="m-0 flex list-none flex-col gap-3 p-0">
            {list.map((hook) => (
              <li
                key={`${hook.event}-${hook.command}`}
                data-testid={`hook-${hook.grant_id}`}
                className="flex flex-wrap items-center gap-3 border border-line px-4 py-3"
              >
                <Badge>{hook.event}</Badge>
                {hook.matcher ? (
                  <span className="text-[0.75rem] text-ink-muted">matcher: {hook.matcher}</span>
                ) : null}
                <code className="min-w-0 flex-1 truncate font-mono text-[0.75rem] text-ink">
                  {hook.command}
                </code>
                {hook.executable ? null : <Badge tone="danger">not executable</Badge>}
                <Badge tone={hook.approved ? "ok" : "warn"}>
                  {hook.approved ? "approved" : "not approved"}
                </Badge>
                <IconButton tone="danger" label="Remove hook" onClick={() => setRemoving(hook)}>
                  <Trash2 />
                </IconButton>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
      {adding ? (
        <HookDialog events={hooks.data?.events ?? []} onClose={() => setAdding(false)} />
      ) : null}
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
    </SystemSection>
  );
}
