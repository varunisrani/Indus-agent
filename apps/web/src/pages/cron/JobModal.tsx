// The cron job panel (W52-c): create or edit one job. Profile (create only), name, prompt, a
// schedule picker that writes our grammar directly, delivery target, skills to load first, and an
// "Advanced fields" box kept as a placeholder (per-job model and script settings do not exist yet).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { api } from "../../lib/api.ts";
import type { CronJob, CronJobInput } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Button, Checkbox, Field, Input, Notice, Select, Textarea } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { Modal } from "../../ui/Dialog.tsx";
import {
  draftProblem,
  draftToSchedule,
  INTERVAL_UNITS,
  type IntervalUnit,
  SCHEDULE_MODES,
  type ScheduleDraft,
  type ScheduleMode,
  scheduleToDraft,
  WEEK_DAYS,
} from "./scheduleBuilder.ts";

/** What the panel starts from: an existing job, or a prefill (a blueprint's prompt and schedule). */
export interface JobModalStart {
  job: CronJob | null;
  /** The profile the job lives in (edit) or the one preselected (create). */
  profile: string;
  prefill?: Partial<CronJobInput>;
}

function SchedulePicker({
  draft,
  onChange,
}: {
  draft: ScheduleDraft;
  onChange: (patch: Partial<ScheduleDraft>) => void;
}) {
  const ids = useId();
  const problem = draftProblem(draft);
  const timeField = (
    <Field label="Time of day" htmlFor={`${ids}-time`}>
      <Input
        id={`${ids}-time`}
        type="time"
        value={draft.time}
        onChange={(e) => onChange({ time: e.currentTarget.value })}
      />
    </Field>
  );
  return (
    <div className="flex flex-col gap-3">
      <Field label="Schedule" htmlFor={`${ids}-mode`}>
        <Select
          id={`${ids}-mode`}
          value={draft.mode}
          onChange={(e) => onChange({ mode: e.currentTarget.value as ScheduleMode })}
        >
          {SCHEDULE_MODES.map((mode) => (
            <option key={mode.value} value={mode.value}>
              {mode.label}
            </option>
          ))}
        </Select>
      </Field>
      {draft.mode === "interval" ? (
        <div className="grid grid-cols-[1fr_1.4fr] gap-3">
          <Field label="Every" htmlFor={`${ids}-every`}>
            <Input
              id={`${ids}-every`}
              type="number"
              min={1}
              value={Number.isNaN(draft.every) ? "" : draft.every}
              onChange={(e) => onChange({ every: Number.parseInt(e.currentTarget.value, 10) })}
            />
          </Field>
          <Field label="Unit" htmlFor={`${ids}-unit`}>
            <Select
              id={`${ids}-unit`}
              value={draft.unit}
              onChange={(e) => onChange({ unit: e.currentTarget.value as IntervalUnit })}
            >
              {INTERVAL_UNITS.map((unit) => (
                <option key={unit.value} value={unit.value}>
                  {unit.label}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      ) : null}
      {draft.mode === "daily" ? timeField : null}
      {draft.mode === "weekly" ? (
        <>
          <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0">
            <legend className="type-label mb-1.5 text-ink-muted">Days</legend>
            <div className="flex flex-wrap gap-1.5">
              {WEEK_DAYS.map((day) => {
                const on = draft.days.includes(day.value);
                return (
                  <Button
                    key={day.value}
                    size="sm"
                    tone={on ? "primary" : "default"}
                    aria-pressed={on}
                    className="min-w-10 uppercase"
                    onClick={() =>
                      onChange({
                        days: on
                          ? draft.days.filter((d) => d !== day.value)
                          : [...draft.days, day.value],
                      })
                    }
                  >
                    {day.label}
                  </Button>
                );
              })}
            </div>
          </fieldset>
          {timeField}
        </>
      ) : null}
      {draft.mode === "monthly" ? (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Day of month" htmlFor={`${ids}-dom`}>
            <Input
              id={`${ids}-dom`}
              type="number"
              min={1}
              max={28}
              value={Number.isNaN(draft.dayOfMonth) ? "" : draft.dayOfMonth}
              onChange={(e) => onChange({ dayOfMonth: Number.parseInt(e.currentTarget.value, 10) })}
            />
          </Field>
          {timeField}
        </div>
      ) : null}
      {draft.mode === "once" ? (
        <Field label="Run at" htmlFor={`${ids}-at`}>
          <Input
            id={`${ids}-at`}
            type="datetime-local"
            value={draft.at}
            onChange={(e) => onChange({ at: e.currentTarget.value })}
          />
        </Field>
      ) : null}
      {draft.mode === "custom" ? (
        <Field
          label="Custom schedule"
          htmlFor={`${ids}-custom`}
          hint="For example: every 2h, in 45m, weekdays at 18:00, or a five-field cron line."
        >
          <Input
            id={`${ids}-custom`}
            value={draft.custom}
            onChange={(e) => onChange({ custom: e.currentTarget.value })}
          />
        </Field>
      ) : null}
      <p className="type-serif m-0 text-[0.75rem] text-ink-soft">
        {problem ? (
          <span className="text-danger">{problem}</span>
        ) : (
          <>
            Sends as:{" "}
            <code className="type-data text-[0.75rem] text-ink">{draftToSchedule(draft)}</code>
          </>
        )}
      </p>
    </div>
  );
}

export function JobModal({
  start,
  profiles,
  onClose,
}: {
  start: JobModalStart;
  profiles: string[];
  onClose: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const ids = useId();
  const { job } = start;
  const [profile, setProfile] = useState(start.profile);
  const [name, setName] = useState(job?.name ?? start.prefill?.name ?? "");
  const [prompt, setPrompt] = useState(job?.prompt ?? start.prefill?.prompt ?? "");
  const [draft, setDraft] = useState<ScheduleDraft>(() => {
    const from = job?.schedule ?? start.prefill?.schedule;
    return from ? scheduleToDraft(from) : scheduleToDraft("every 30m");
  });
  const [deliver, setDeliver] = useState(job?.deliver ?? start.prefill?.deliver ?? "local");
  const [skills, setSkills] = useState<string[]>(job?.skills ?? start.prefill?.skills ?? []);
  const [problem, setProblem] = useState("");

  const targets = useQuery({
    queryKey: ["cron", "targets", profile],
    queryFn: () => api.cron.targets(profile || undefined),
  });
  const skillList = useQuery({
    queryKey: ["skills", "installed", "for-cron", profile],
    queryFn: () => api.skills.list(profile || undefined),
  });

  const save = useMutation({
    mutationFn: (body: CronJobInput) =>
      job
        ? api.cron.update(job.id, body, profile || undefined)
        : api.cron.create(body, profile || undefined),
    onSuccess: () => {
      toast.show(job ? "Job updated." : "Job created.", "success");
      void client.invalidateQueries({ queryKey: ["cron"] });
      onClose();
    },
    onError: (error) => setProblem(errorText(error)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!prompt.trim()) return setProblem("Write the prompt the job should run.");
    const scheduleProblem = draftProblem(draft);
    if (scheduleProblem) return setProblem(scheduleProblem);
    setProblem("");
    const body: CronJobInput = {
      name: name.trim(),
      prompt,
      schedule: draftToSchedule(draft),
      deliver,
    };
    // A new job leaves skills out when none are picked; an edit always sends the list so
    // unticking every box clears it.
    if (job || skills.length) body.skills = skills;
    save.mutate(body);
  };

  const targetList = targets.data?.targets ?? [];
  const onlyLocal =
    targetList.length > 0 && targetList.every((t) => t.kind === "local" || !t.available);
  const installed = skillList.data?.skills ?? [];

  return (
    <Modal
      open
      size="lg"
      // A pale veil of the page colour, not the grey scrim, as the reference's form panels (W54-W1-4).
      overlayClassName="bg-canvas/85"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={job ? "Edit job" : "New cron job"}
    >
      <form className="flex flex-col gap-4 py-1" onSubmit={submit} noValidate>
        {job ? null : (
          <Field label="Profile" htmlFor={`${ids}-profile`}>
            <Select
              id={`${ids}-profile`}
              value={profile}
              onChange={(e) => setProfile(e.currentTarget.value)}
            >
              {(profiles.includes(profile) ? profiles : [profile, ...profiles]).map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field label="Name (optional)" htmlFor={`${ids}-name`}>
          <Input
            id={`${ids}-name`}
            autoFocus
            placeholder="e.g. Daily summary"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <Field label="Prompt" htmlFor={`${ids}-prompt`}>
          <Textarea
            id={`${ids}-prompt`}
            className="min-h-20 resize-y"
            rows={3}
            placeholder="What the agent should do on each run."
            value={prompt}
            onChange={(e) => setPrompt(e.currentTarget.value)}
          />
        </Field>
        <SchedulePicker draft={draft} onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))} />
        <Field label="Deliver to" htmlFor={`${ids}-deliver`}>
          <Select
            id={`${ids}-deliver`}
            value={deliver}
            onChange={(e) => setDeliver(e.currentTarget.value)}
          >
            {targetList.length === 0 ? <option value="local">Local</option> : null}
            {targetList.map((t) => (
              <option key={t.id} value={t.id} disabled={!t.available}>
                {t.label}
                {t.available ? "" : " (set up first)"}
              </option>
            ))}
          </Select>
          {onlyLocal ? (
            <p className="m-0 text-[0.6875rem] text-ink-soft">
              Connect a channel to send results somewhere other than this machine.
            </p>
          ) : null}
        </Field>
        <div className="flex flex-col gap-1.5">
          <span className="type-label text-ink-muted" id={`${ids}-skills`}>
            Skills (optional)
          </span>
          <ul
            aria-labelledby={`${ids}-skills`}
            className="m-0 max-h-36 list-none overflow-y-auto border border-line p-0"
          >
            {installed.length === 0 ? (
              <li className="px-3 py-2 text-[0.75rem] text-ink-soft">
                {skillList.isPending ? "Loading…" : "No skills installed."}
              </li>
            ) : (
              installed.map((skill) => (
                <li key={skill.name}>
                  <label
                    htmlFor={`${ids}-skill-${skill.name}`}
                    className="flex cursor-pointer items-center gap-2 px-3 py-1.5 hover:bg-hover"
                  >
                    <Checkbox
                      id={`${ids}-skill-${skill.name}`}
                      checked={skills.includes(skill.name)}
                      onChange={(e) => {
                        const on = e.currentTarget.checked;
                        setSkills((list) =>
                          on ? [...list, skill.name] : list.filter((n) => n !== skill.name),
                        );
                      }}
                    />
                    <span className="type-data text-[0.75rem] text-ink">{skill.name}</span>
                  </label>
                </li>
              ))
            )}
          </ul>
          <p className="type-serif m-0 text-[0.75rem] text-ink-soft">
            Ticked skills are loaded before the prompt runs.
          </p>
        </div>
        <details open className="border border-line p-3">
          <summary className="type-label cursor-pointer text-ink-muted">Advanced fields</summary>
          <p className="m-0 mt-2 text-[0.75rem] text-ink-soft">
            Per-job model, provider and script settings are not available yet.
          </p>
        </details>
        {problem ? <Notice tone="danger">{problem}</Notice> : null}
        <div className={cx("flex items-center gap-2", job ? "justify-between" : "justify-end")}>
          {job ? <code className="type-data text-[0.75rem] text-ink-soft">{job.id}</code> : null}
          <Button
            type="submit"
            tone="primary"
            size="sm"
            className="uppercase"
            disabled={save.isPending}
            icon={save.isPending ? <LoaderCircle className="animate-spin" /> : undefined}
          >
            {save.isPending ? (job ? "Saving…" : "Creating…") : job ? "Save changes" : "Create"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
