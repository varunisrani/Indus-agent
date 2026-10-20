// Cron (P18-22): scheduled prompts. Create and edit in a modal with a schedule helper; pause,
// resume, run now and delete from the list.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { api } from "../lib/api.ts";
import { relativeTime } from "../lib/format.ts";
import type { CronJob, CronJobInput, CronState, DeliveryTarget } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  type BadgeTone,
  Button,
  Field,
  inputClass,
  Notice,
  QueryState,
} from "../ui/controls.tsx";
import { ConfirmDialog, Modal } from "../ui/Dialog.tsx";
import {
  BUILDER_KINDS,
  BUILDER_UNITS,
  type BuilderChoice,
  type BuilderKind,
  type BuilderUnit,
  builderProblem,
  buildSchedule,
  DEFAULT_CHOICE,
} from "./cron/scheduleBuilder.ts";

const STATE_TONE: Record<CronState, BadgeTone> = {
  scheduled: "ok",
  paused: "warn",
  running: "accent",
  completed: "neutral",
  error: "danger",
};

export function ScheduleBuilder({ onUse }: { onUse: (schedule: string) => void }) {
  const id = useId();
  const [choice, setChoice] = useState<BuilderChoice>(DEFAULT_CHOICE);
  const problem = builderProblem(choice);
  const update = (patch: Partial<BuilderChoice>) => setChoice((c) => ({ ...c, ...patch }));
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-line p-3">
      <legend className="px-1 text-xs text-muted">Schedule helper</legend>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-muted">
          Kind
          <select
            className={inputClass}
            value={choice.kind}
            onChange={(e) => update({ kind: e.currentTarget.value as BuilderKind })}
          >
            {BUILDER_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        {choice.kind === "every" || choice.kind === "in" ? (
          <>
            <label className="flex flex-col gap-1 text-xs text-muted">
              Amount
              <input
                className={`${inputClass} w-20`}
                type="number"
                min={1}
                value={Number.isNaN(choice.amount) ? "" : choice.amount}
                onChange={(e) => update({ amount: Number.parseInt(e.currentTarget.value, 10) })}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              Unit
              <select
                className={inputClass}
                value={choice.unit}
                onChange={(e) => update({ unit: e.currentTarget.value as BuilderUnit })}
              >
                {BUILDER_UNITS.map((u) => (
                  <option key={u.value} value={u.value}>
                    {u.label}
                  </option>
                ))}
              </select>
            </label>
          </>
        ) : null}
        {choice.kind === "daily" || choice.kind === "weekdays" ? (
          <label className="flex flex-col gap-1 text-xs text-muted">
            Time
            <input
              className={inputClass}
              value={choice.time}
              onChange={(e) => update({ time: e.currentTarget.value })}
              placeholder="09:00"
            />
          </label>
        ) : null}
        {choice.kind === "cron" ? (
          <label className="flex flex-1 flex-col gap-1 text-xs text-muted">
            Expression
            <input
              className={`${inputClass} font-mono`}
              value={choice.cron}
              onChange={(e) => update({ cron: e.currentTarget.value })}
            />
          </label>
        ) : null}
        <Button
          disabled={problem !== null}
          onClick={() => onUse(buildSchedule(choice))}
          aria-describedby={`${id}-preview`}
        >
          Use
        </Button>
      </div>
      <p id={`${id}-preview`} className="text-xs text-muted">
        {problem ?? (
          <>
            Will set: <code className="font-mono">{buildSchedule(choice)}</code>
          </>
        )}
      </p>
    </fieldset>
  );
}

function JobModal({
  job,
  targets,
  onClose,
}: {
  job: CronJob | null;
  targets: DeliveryTarget[];
  onClose: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [input, setInput] = useState<CronJobInput>({
    name: job?.name ?? "",
    prompt: job?.prompt ?? "",
    schedule: job?.schedule ?? "",
    deliver: job?.deliver ?? "local",
  });
  const [problem, setProblem] = useState("");
  const save = useMutation({
    mutationFn: (body: CronJobInput) =>
      job ? api.cron.update(job.id, body) : api.cron.create(body),
    onSuccess: () => {
      toast.show(job ? "Job updated." : "Job created.", "success");
      void client.invalidateQueries({ queryKey: ["cron"] });
      onClose();
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!input.prompt.trim()) return setProblem("Write the prompt the job should run.");
    if (!input.schedule.trim()) return setProblem("Give the job a schedule.");
    setProblem("");
    save.mutate({ ...input, name: input.name.trim(), schedule: input.schedule.trim() });
  };

  const set = (patch: Partial<CronJobInput>) => setInput((i) => ({ ...i, ...patch }));
  return (
    <Modal
      open
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={job ? `Edit ${job.name || "job"}` : "New scheduled job"}
      description="A prompt the agent runs on a schedule, with its answer delivered where you choose."
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        <Field label="Name (optional)" htmlFor={`${ids}-name`}>
          <input
            id={`${ids}-name`}
            className={inputClass}
            value={input.name}
            onChange={(e) => set({ name: e.currentTarget.value })}
          />
        </Field>
        <Field label="Prompt" htmlFor={`${ids}-prompt`}>
          <textarea
            id={`${ids}-prompt`}
            className={inputClass}
            rows={4}
            value={input.prompt}
            onChange={(e) => set({ prompt: e.currentTarget.value })}
          />
        </Field>
        <Field
          label="Schedule"
          htmlFor={`${ids}-schedule`}
          hint="For example: every 30m, in 2h, daily at 09:00, or a cron line such as 0 9 * * 1-5."
        >
          <input
            id={`${ids}-schedule`}
            className={`${inputClass} font-mono`}
            value={input.schedule}
            onChange={(e) => set({ schedule: e.currentTarget.value })}
          />
        </Field>
        <ScheduleBuilder onUse={(schedule) => set({ schedule })} />
        <Field label="Deliver to" htmlFor={`${ids}-deliver`}>
          <select
            id={`${ids}-deliver`}
            className={inputClass}
            value={input.deliver}
            onChange={(e) => set({ deliver: e.currentTarget.value })}
          >
            {targets.map((t) => (
              <option key={t.id} value={t.id} disabled={!t.available}>
                {t.label}
                {t.available ? "" : " (not set up)"}
              </option>
            ))}
          </select>
        </Field>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" tone="primary" disabled={save.isPending}>
            {job ? "Save" : "Create"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function CronPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [editing, setEditing] = useState<CronJob | "new" | null>(null);
  const [deleting, setDeleting] = useState<CronJob | null>(null);
  const jobs = useQuery({ queryKey: ["cron", "jobs"], queryFn: () => api.cron.jobs() });
  const targets = useQuery({ queryKey: ["cron", "targets"], queryFn: () => api.cron.targets() });

  const act = useMutation({
    mutationFn: async ({
      job,
      action,
    }: {
      job: CronJob;
      action: "pause" | "resume" | "trigger" | "remove";
    }) => {
      await api.cron[action](job.id);
    },
    onSuccess: (_reply, { job, action }) => {
      const label = job.name || job.id;
      const done = { pause: "paused", resume: "resumed", trigger: "started", remove: "deleted" }[
        action
      ];
      toast.show(`${label} ${done}.`, "success");
      if (action === "remove") setDeleting(null);
      void client.invalidateQueries({ queryKey: ["cron", "jobs"] });
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const list = jobs.data?.jobs ?? [];
  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Button tone="primary" onClick={() => setEditing("new")}>
          New job
        </Button>
      </HeaderActions>
      <QueryState
        loading={jobs.isPending}
        error={jobs.error}
        empty={list.length === 0}
        emptyText="No scheduled jobs yet."
      >
        <ul className="flex flex-col gap-2" aria-label="Scheduled jobs">
          {list.map((job) => (
            <li
              key={job.id}
              className="flex flex-col gap-2 rounded-md border border-line bg-raised p-3"
              data-testid={`job-${job.id}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex-1 text-sm font-medium">
                  {job.name || job.prompt.slice(0, 60)}
                </span>
                <Badge tone={STATE_TONE[job.state]}>{job.state}</Badge>
                <code className="font-mono text-xs">{job.schedule_display}</code>
              </div>
              <p className="text-xs text-muted">
                Last run {relativeTime(job.last_run_at)}
                {job.last_status ? ` (${job.last_status})` : ""} · next{" "}
                {job.next_run_at ? relativeTime(job.next_run_at) : "not planned"} · to {job.deliver}
              </p>
              <div className="flex flex-wrap gap-1">
                {job.enabled ? (
                  <Button tone="ghost" onClick={() => act.mutate({ job, action: "pause" })}>
                    Pause
                  </Button>
                ) : (
                  <Button tone="ghost" onClick={() => act.mutate({ job, action: "resume" })}>
                    Resume
                  </Button>
                )}
                <Button tone="ghost" onClick={() => act.mutate({ job, action: "trigger" })}>
                  Run now
                </Button>
                <Button tone="ghost" onClick={() => setEditing(job)}>
                  Edit
                </Button>
                <Button tone="ghost" onClick={() => setDeleting(job)}>
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </QueryState>
      {editing ? (
        <JobModal
          job={editing === "new" ? null : editing}
          targets={targets.data?.targets ?? []}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name || "this job"}?`}
        description="The job stops running and its schedule is removed."
        confirmLabel="Delete"
        danger
        busy={act.isPending}
        onConfirm={() => {
          if (deleting) act.mutate({ job: deleting, action: "remove" });
        }}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}
