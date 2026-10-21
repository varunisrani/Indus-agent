// Scheduled jobs (P24-27): the P14 cron jobs of this profile, or of every profile at once
// ("All profiles": one `GET /api/cron/jobs` per profile, each row tagged with its owner). Every
// action — pause, resume, run now, edit, delete — goes to the profile that owns the job, never to
// the window's own. New jobs start blank or from a blueprint (a skill that suggests a schedule).
// The last run's time, status and (redacted) error are the run history the REST view carries.

import { useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { LoadState, ManageOverlay, useApi, useRestValue, when } from "../manage/common.tsx";

export interface CronJob {
  id: string;
  name: string;
  prompt: string;
  schedule: string;
  schedule_display: string;
  deliver: string;
  skills: string[];
  state: string;
  enabled: boolean;
  paused_reason: string | null;
  last_run_at: string | null;
  next_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
}

export interface OwnedJob extends CronJob {
  /** The profile that owns the job (actions go there). */
  profile: string;
}

export interface Blueprint {
  skill: string;
  description: string;
  schedule: string | null;
  prompt: string | null;
}

interface ProfilesAnswer {
  profiles: Array<{ name: string }>;
}

/** The jobs of `profiles` (each tagged); profiles that fail are named, the rest still listed. */
export async function loadJobs(
  api: RestLike | null,
  profiles: readonly string[],
): Promise<RestAnswer<{ jobs: OwnedJob[]; failed: string[] }>> {
  const answers = await Promise.all(
    profiles.map((profile) => rest<{ jobs: CronJob[] }>(api, "GET", "/api/cron/jobs", { profile })),
  );
  const jobs: OwnedJob[] = [];
  const failed: string[] = [];
  answers.forEach((answer, index) => {
    const profile = profiles[index] as string;
    if (answer.ok) for (const job of answer.data.jobs) jobs.push({ ...job, profile });
    else failed.push(profile);
  });
  if (failed.length === profiles.length && answers[0] !== undefined && !answers[0].ok) {
    return answers[0] as RestAnswer<never>;
  }
  return { ok: true, status: 200, data: { jobs, failed } };
}

/** Send `action` for `job` to its owning profile. */
export function jobAction(
  api: RestLike | null,
  job: OwnedJob,
  action: "pause" | "resume" | "trigger" | "delete",
): Promise<RestAnswer<unknown>> {
  const base = `/api/cron/jobs/${encodeURIComponent(job.id)}`;
  if (action === "delete") return rest(api, "DELETE", base, { profile: job.profile });
  return rest(api, "POST", `${base}/${action}`, { profile: job.profile });
}

function JobForm(props: {
  api: RestLike | null;
  profile: string;
  job?: OwnedJob;
  blueprints: Blueprint[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  const [name, setName] = useState(props.job?.name ?? "");
  const [prompt, setPrompt] = useState(props.job?.prompt ?? "");
  const [schedule, setSchedule] = useState(props.job?.schedule ?? "");
  const [deliver, setDeliver] = useState(props.job?.deliver ?? "");
  const [skills, setSkills] = useState<string[]>(props.job?.skills ?? []);
  const [error, setError] = useState("");
  const submit = async () => {
    const body = {
      name: name.trim() || undefined,
      prompt,
      schedule,
      ...(deliver.trim() === "" ? {} : { deliver: deliver.trim() }),
      ...(skills.length > 0 ? { skills } : {}),
    };
    const answer =
      props.job === undefined
        ? await rest(props.api, "POST", "/api/cron/jobs", { profile: props.profile, body })
        : await rest(props.api, "PUT", `/api/cron/jobs/${encodeURIComponent(props.job.id)}`, {
            profile: props.job.profile,
            body,
          });
    if (!answer.ok) {
      setError(answer.message);
      return;
    }
    props.onDone();
  };
  return (
    <form
      className="manage-card"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h3>{props.job === undefined ? t("manage.cron.new") : t("manage.cron.edit")}</h3>
      {props.job === undefined && props.blueprints.length > 0 && (
        <label className="manage-field">
          {t("manage.cron.blueprint")}
          <select
            defaultValue=""
            onChange={(e) => {
              const plan = props.blueprints.find((b) => b.skill === e.target.value);
              if (plan === undefined) return;
              setName(plan.skill);
              setPrompt(plan.prompt ?? plan.description);
              setSchedule(plan.schedule ?? "");
              setSkills([plan.skill]);
            }}
          >
            <option value="">{t("manage.cron.blank")}</option>
            {props.blueprints.map((plan) => (
              <option key={plan.skill} value={plan.skill}>
                {plan.skill}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="manage-field">
        {t("manage.cron.name")}
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="manage-field">
        {t("manage.cron.prompt")}
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} required />
      </label>
      <label className="manage-field">
        {t("manage.cron.schedule")}
        <input
          value={schedule}
          onChange={(e) => setSchedule(e.target.value)}
          placeholder="every 2h"
          required
        />
      </label>
      <label className="manage-field">
        {t("manage.cron.deliver")}
        <input value={deliver} onChange={(e) => setDeliver(e.target.value)} placeholder="local" />
      </label>
      {error !== "" && (
        <p className="manage-error" role="alert">
          {error}
        </p>
      )}
      <div className="manage-inline">
        <button type="submit" className="button button-primary">
          {t("manage.save")}
        </button>
        <button type="button" className="button button-secondary" onClick={props.onCancel}>
          {t("manage.cancel")}
        </button>
      </div>
    </form>
  );
}

export function CronOverlay(props: {
  api?: RestLike | null;
  profile: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const api = useApi(props.api);
  const own = props.profile ?? "default";
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState<OwnedJob | "new" | null>(null);
  const [note, setNote] = useState("");
  const profiles = useRestValue<ProfilesAnswer>(() => rest(api, "GET", "/api/profiles"), [api]);
  const names = all && profiles.state === "ok" ? profiles.data.profiles.map((p) => p.name) : [own];
  const jobs = useRestValue(() => loadJobs(api, names), [api, names.join("\n")]);
  const blueprints = useRestValue<{ blueprints: Blueprint[] }>(
    () => rest(api, "GET", "/api/cron/blueprints", { profile: own }),
    [api, own],
  );

  const act = async (job: OwnedJob, action: "pause" | "resume" | "trigger" | "delete") => {
    const answer = await jobAction(api, job, action);
    setNote(answer.ok ? "" : answer.message);
    jobs.reload();
  };

  return (
    <ManageOverlay title={t("nav.cron")} onClose={props.onClose}>
      <div className="manage-inline">
        <label className="manage-inline">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
          {t("manage.cron.allProfiles")}
        </label>
        <button type="button" className="button button-primary" onClick={() => setEditing("new")}>
          {t("manage.cron.new")}
        </button>
      </div>
      {note !== "" && (
        <p className="manage-error" role="alert">
          {note}
        </p>
      )}
      {editing !== null && (
        <JobForm
          api={api}
          profile={own}
          {...(editing === "new" ? {} : { job: editing })}
          blueprints={blueprints.state === "ok" ? blueprints.data.blueprints : []}
          onDone={() => {
            setEditing(null);
            jobs.reload();
          }}
          onCancel={() => setEditing(null)}
        />
      )}
      <LoadState value={jobs}>
        {(data) => (
          <>
            {data.failed.length > 0 && (
              <p className="manage-muted">
                {t("manage.cron.someFailed", { names: data.failed.join(", ") })}
              </p>
            )}
            <table className="manage-table">
              <thead>
                <tr>
                  <th>{t("manage.cron.name")}</th>
                  {all && <th>{t("manage.cron.profile")}</th>}
                  <th>{t("manage.cron.schedule")}</th>
                  <th>{t("manage.cron.lastRun")}</th>
                  <th>{t("manage.cron.next")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.jobs.map((job) => (
                  <tr key={`${job.profile}/${job.id}`} data-job={job.id}>
                    <td>
                      <strong>{job.name || job.id}</strong>
                      <div className="manage-muted">{job.state}</div>
                    </td>
                    {all && <td>{job.profile}</td>}
                    <td>{job.schedule_display}</td>
                    <td>
                      {when(job.last_run_at)}
                      {job.last_status !== null && (
                        <div className="manage-muted">{job.last_status}</div>
                      )}
                      {job.last_error !== null && (
                        <div className="manage-error">{job.last_error}</div>
                      )}
                    </td>
                    <td>{when(job.next_run_at)}</td>
                    <td>
                      <span className="manage-inline">
                        {job.state === "paused" || !job.enabled ? (
                          <button
                            type="button"
                            className="button button-secondary"
                            onClick={() => void act(job, "resume")}
                          >
                            {t("manage.cron.resume")}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="button button-secondary"
                            onClick={() => void act(job, "pause")}
                          >
                            {t("manage.cron.pause")}
                          </button>
                        )}
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => void act(job, "trigger")}
                        >
                          {t("manage.cron.runNow")}
                        </button>
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => setEditing(job)}
                        >
                          {t("manage.cron.edit")}
                        </button>
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => void act(job, "delete")}
                        >
                          {t("manage.delete")}
                        </button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.jobs.length === 0 && <p className="manage-muted">{t("manage.cron.none")}</p>}
          </>
        )}
      </LoadState>
    </ManageOverlay>
  );
}
