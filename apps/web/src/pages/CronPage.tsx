// Cron (P18-22, look W52-c): scheduled prompts. A Jobs / Blueprints strip; jobs listed as cards
// with icon actions (pause or resume, run now, edit, delete) and filtered by profile ("All
// profiles" asks each profile and merges); create and edit in the job panel.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock, Pause, Pencil, Play, Trash2, Zap } from "lucide-react";
import { useId, useState } from "react";
import { api } from "../lib/api.ts";
import type { CronBlueprint, CronJob, CronState } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import {
  Badge,
  type BadgeTone,
  Button,
  Card,
  ErrorState,
  IconButton,
  Loading,
  SectionHeading,
  Segmented,
  Select,
} from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { Blueprints } from "./cron/Blueprints.tsx";
import { JobModal, type JobModalStart } from "./cron/JobModal.tsx";

const STATE_TONE: Record<CronState, BadgeTone> = {
  scheduled: "ok",
  paused: "warn",
  running: "accent",
  completed: "danger",
  error: "danger",
};

type View = "jobs" | "blueprints";
type Action = "pause" | "resume" | "trigger" | "remove";

/** A job with the profile it was read from. */
interface ProfiledJob extends CronJob {
  profile: string;
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function repeatText(job: CronJob): string {
  const times = job.repeat?.times ?? null;
  return times === null ? "forever" : `${job.repeat?.completed ?? 0}/${times}`;
}

function JobCard({
  job,
  onAct,
  onEdit,
  onDelete,
}: {
  job: ProfiledJob;
  onAct: (action: Action) => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const skills = job.skills ?? [];
  const overdue =
    job.state === "scheduled" &&
    job.next_run_at !== null &&
    Date.parse(job.next_run_at) < Date.now();
  return (
    <li
      className="flex items-start gap-3 border border-line px-4 py-3.5"
      data-testid={`job-${job.id}`}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.875rem] font-medium text-ink">
            {job.name || job.prompt.slice(0, 60)}
          </span>
          <Badge tone={STATE_TONE[job.state]}>{job.state}</Badge>
          {job.profile ? <Badge>{job.profile}</Badge> : null}
          {job.deliver && job.deliver !== "local" ? <Badge>to: {job.deliver}</Badge> : null}
          {skills.length ? (
            <Badge>{skills.length === 1 ? skills[0] : `${skills.length} skills`}</Badge>
          ) : null}
          {job.last_status && job.last_status !== "ok" ? (
            <Badge tone="danger">last: {job.last_status}</Badge>
          ) : null}
        </div>
        {job.name ? (
          <p className="m-0 truncate text-[0.75rem] text-ink-muted">{job.prompt}</p>
        ) : null}
        <p className="m-0 flex flex-wrap gap-x-4 gap-y-1 text-[0.75rem] text-ink-muted">
          <code className="type-data text-[0.75rem] text-ink">{job.schedule_display}</code>
          <span>repeat: {repeatText(job)}</span>
          <span>Last: {when(job.last_run_at)}</span>
          <span className={overdue ? "text-warning" : undefined}>
            {overdue ? "Overdue since" : "Next"}: {when(job.next_run_at)}
          </span>
        </p>
        {job.last_error ? (
          <p className="m-0 text-[0.6875rem] break-words text-danger">{job.last_error}</p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {job.enabled ? (
          <IconButton label="Pause" onClick={() => onAct("pause")}>
            <Pause className="text-warning" />
          </IconButton>
        ) : (
          <IconButton label="Resume" onClick={() => onAct("resume")}>
            <Play className="text-success" />
          </IconButton>
        )}
        <IconButton label="Run now" onClick={() => onAct("trigger")}>
          <Zap />
        </IconButton>
        <IconButton label="Edit" onClick={onEdit}>
          <Pencil />
        </IconButton>
        <IconButton label="Delete" tone="danger" onClick={onDelete}>
          <Trash2 />
        </IconButton>
      </div>
    </li>
  );
}

export function CronPage() {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const [view, setView] = useState<View>("jobs");
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<JobModalStart | null>(null);
  const [deleting, setDeleting] = useState<ProfiledJob | null>(null);

  const profiles = useQuery({ queryKey: ["profiles", "list"], queryFn: () => api.profiles() });
  const names = profiles.data?.profiles.map((p) => p.name) ?? [];
  const home = profiles.data?.current ?? profiles.data?.active ?? "";

  const jobs = useQuery({
    queryKey: ["cron", "jobs", filter, names.join("\n")],
    enabled: !profiles.isPending,
    queryFn: async (): Promise<ProfiledJob[]> => {
      if (filter) {
        const reply = await api.cron.jobs(filter);
        return reply.jobs.map((job) => ({ ...job, profile: filter }));
      }
      if (names.length === 0) {
        const reply = await api.cron.jobs();
        return reply.jobs.map((job) => ({ ...job, profile: home }));
      }
      // "All profiles": one request per profile, merged; a profile that fails is skipped
      // unless every one fails.
      const replies = await Promise.allSettled(names.map((name) => api.cron.jobs(name)));
      const merged: ProfiledJob[] = [];
      let failure: unknown = null;
      replies.forEach((reply, i) => {
        if (reply.status === "fulfilled")
          for (const job of reply.value.jobs) merged.push({ ...job, profile: names[i] ?? "" });
        else failure = reply.reason;
      });
      if (failure !== null && merged.length === 0 && replies.every((r) => r.status === "rejected"))
        throw failure;
      return merged;
    },
  });

  const act = useMutation({
    mutationFn: async ({ job, action }: { job: ProfiledJob; action: Action }) => {
      await api.cron[action](job.id, job.profile || undefined);
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

  const list = jobs.data ?? [];
  const create = (prefill?: JobModalStart["prefill"], blueprint?: CronBlueprint) =>
    setEditing({
      job: null,
      profile: filter || home || "default",
      ...(prefill ? { prefill } : {}),
      ...(blueprint ? { blueprint } : {}),
    });

  return (
    <div className="flex flex-col gap-6 px-4 py-[1.4375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderActions>
        <Button tone="primary" size="sm" className="uppercase" onClick={() => create()}>
          Create
        </Button>
      </HeaderActions>
      {jobs.error ? (
        // A failed load is a banner at the top of the page, as the reference shows it (W54-W1-6).
        <ErrorState
          message="Could not load the cron jobs. Check that the dashboard is running and press Retry."
          details={errorText(jobs.error)}
          onRetry={() => void jobs.refetch()}
        />
      ) : null}
      <Segmented
        label="Cron view"
        fill
        value={view}
        options={[
          { value: "jobs", label: "Jobs" },
          { value: "blueprints", label: "Blueprints" },
        ]}
        onChange={setView}
      />

      {view === "jobs" ? (
        <section aria-label="Jobs" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <SectionHeading icon={Clock}>Scheduled Jobs ({list.length})</SectionHeading>
            <div className="flex w-full flex-col gap-1.5 sm:w-[13.75rem]">
              <label htmlFor={`${ids}-profile`} className="type-label text-ink-muted">
                Profile
              </label>
              <Select
                id={`${ids}-profile`}
                value={filter}
                onChange={(e) => setFilter(e.currentTarget.value)}
              >
                <option value="">All profiles</option>
                {names.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          {jobs.isPending && !jobs.error ? (
            <div className="flex justify-center py-24">
              <Loading />
            </div>
          ) : list.length === 0 && !jobs.error ? (
            <Card className="flex flex-col items-center gap-3 py-8 text-center">
              <p className="m-0 text-[0.8125rem] text-ink-muted">No scheduled jobs yet.</p>
              <Button tone="primary" size="sm" className="uppercase" onClick={() => create()}>
                Create job
              </Button>
            </Card>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Scheduled jobs">
              {list.map((job) => (
                <JobCard
                  key={`${job.profile}/${job.id}`}
                  job={job}
                  onAct={(action) => act.mutate({ job, action })}
                  onEdit={() => setEditing({ job, profile: job.profile })}
                  onDelete={() => setDeleting(job)}
                />
              ))}
            </ul>
          )}
        </section>
      ) : (
        <Blueprints
          onSetUp={(blueprint) =>
            create(
              {
                name: blueprint.title,
                prompt: blueprint.prompt ?? "",
                ...(blueprint.schedule ? { schedule: blueprint.schedule } : {}),
                skills: blueprint.skill ? [blueprint.skill] : [],
              },
              blueprint,
            )
          }
        />
      )}

      {editing ? (
        <JobModal
          start={editing}
          profiles={names.length ? names : [editing.profile]}
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
