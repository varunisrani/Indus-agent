// Scheduled jobs (P24-27): the P14 cron jobs of this profile, or of every profile at once
// ("All profiles": one `GET /api/cron/jobs` per profile, each row tagged with its owner). Every
// action — pause, resume, run now, edit, delete — goes to the profile that owns the job, never to
// the window's own. New jobs start blank or from a blueprint (a skill that suggests a schedule).
// The last run's time, status and (redacted) error are the run history the REST view carries.
// W52-l look: an overlay panel (list + detail) and a 512px dialog for new / edited jobs; the
// "All profiles" switch stays (ours, W52-l-5) as a small checkbox under the search.

import { Pause, Pencil, Play, Rocket, Watch, Zap } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestAnswer, type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button } from "../../ui/components.tsx";
import {
  CapsLabel,
  DetailHead,
  InlineError,
  MetaGrid,
  PanelBlock,
  PanelPill,
  PanelRow,
} from "../kit.tsx";
import { Loader } from "../Loader.tsx";
import { useApi, useRestValue, when } from "../manage/common.tsx";
import { ListDetail, OverlayCard } from "../overlay/OverlayFrame.tsx";
import type { DeliveryTarget } from "../webhooks/WebhooksPage.tsx";
import { CronDialog } from "./CronDialog.tsx";
import "./cron.css";

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

function stateTone(job: OwnedJob): "info" | "warn" | "muted" {
  if (job.state === "paused" || !job.enabled) return "muted";
  if (job.last_status === "error" || job.last_error !== null) return "warn";
  return "info";
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
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [dialog, setDialog] = useState<
    { kind: "new"; blueprint?: Blueprint } | { kind: "edit"; job: OwnedJob } | null
  >(null);
  const [note, setNote] = useState("");
  const profiles = useRestValue<ProfilesAnswer>(() => rest(api, "GET", "/api/profiles"), [api]);
  const names = all && profiles.state === "ok" ? profiles.data.profiles.map((p) => p.name) : [own];
  const jobs = useRestValue(() => loadJobs(api, names), [api, names.join("\n")]);
  const blueprints = useRestValue<{ blueprints: Blueprint[] }>(
    () => rest(api, "GET", "/api/cron/blueprints", { profile: own }),
    [api, own],
  );
  const targets = useRestValue<{ targets: DeliveryTarget[] }>(
    () => rest(api, "GET", "/api/cron/delivery-targets", { profile: own }),
    [api, own],
  );

  const act = async (job: OwnedJob, action: "pause" | "resume" | "trigger" | "delete") => {
    const answer = await jobAction(api, job, action);
    setNote(answer.ok ? "" : answer.message);
    if (answer.ok && action === "delete") setPicked(null);
    jobs.reload();
  };
  const plans = blueprints.state === "ok" ? blueprints.data.blueprints : [];
  const keyOf = (job: OwnedJob) => `${job.profile}/${job.id}`;

  let body: ReactNode;
  if (jobs.state === "loading") body = <Loader />;
  else if (jobs.state === "error") {
    body = (
      <div className="cron-pad">
        <InlineError>
          {jobs.code === "not-connected" ? t("manage.notConnected") : jobs.message}
        </InlineError>
        <Button variant="secondary" size="sm" onClick={jobs.reload}>
          {t("manage.retry")}
        </Button>
      </div>
    );
  } else {
    const data = jobs.data;
    const needle = query.trim().toLowerCase();
    const shown = data.jobs.filter(
      (job) =>
        needle === "" ||
        (job.name || job.id).toLowerCase().includes(needle) ||
        job.prompt.toLowerCase().includes(needle),
    );
    const job = data.jobs.find((j) => keyOf(j) === picked) ?? null;
    const paused = job !== null && (job.state === "paused" || !job.enabled);
    const newButton = (
      <Button variant="primary" size="sm" onClick={() => setDialog({ kind: "new" })}>
        {t("cron.new")}
      </Button>
    );
    body = (
      <ListDetail
        title={t("nav.cron")}
        subtitle={t(data.jobs.length === 1 ? "cron.countOne" : "cron.count", {
          count: data.jobs.length,
        })}
        whole={data.jobs.length === 0 && plans.length === 0 && !all}
        banner={
          <>
            {note !== "" && <InlineError>{note}</InlineError>}
            {data.failed.length > 0 && (
              <p className="cron-note">
                {t("manage.cron.someFailed", { names: data.failed.join(", ") })}
              </p>
            )}
          </>
        }
        search={{
          label: t("cron.search"),
          placeholder: t("cron.searchPlaceholder"),
          value: query,
          onChange: setQuery,
        }}
        listTop={
          <label className="cron-all">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
            {t("manage.cron.allProfiles")}
          </label>
        }
        add={{ label: t("cron.new"), onAdd: () => setDialog({ kind: "new" }) }}
        listLabel={t("nav.cron")}
        listEmpty={
          data.jobs.length === 0 ? <p className="cron-list-empty">{t("cron.none")}</p> : undefined
        }
        list={shown.map((j) => (
          <PanelRow
            key={keyOf(j)}
            title={j.name || j.id}
            dot={j.state === "paused" || !j.enabled ? "off" : "on"}
            selected={keyOf(j) === picked}
            onSelect={() => setPicked(keyOf(j))}
            data={{ "data-job": j.id }}
            {...(all ? { meta: j.profile } : {})}
            menu={{
              label: t("cron.rowMenu", { name: j.name || j.id }),
              items: [
                {
                  id: "edit",
                  label: t("manage.cron.edit"),
                  run: () => setDialog({ kind: "edit", job: j }),
                },
                {
                  id: "delete",
                  label: t("manage.delete"),
                  tone: "danger",
                  run: () => void act(j, "delete"),
                },
              ],
            }}
          />
        ))}
        listFooter={
          plans.length > 0 ? (
            <section className="cron-plans">
              <h3 className="cron-plans-label">{t("cron.blueprints")}</h3>
              <ul>
                {plans.map((plan) => (
                  <li key={plan.skill}>
                    <button
                      type="button"
                      className="cron-plan"
                      title={plan.description}
                      onClick={() => setDialog({ kind: "new", blueprint: plan })}
                    >
                      <Rocket aria-hidden="true" size={12} />
                      <span>{plan.skill}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ) : undefined
        }
        {...(job === null
          ? {}
          : {
              detail: (
                <div className="cron-detail">
                  <DetailHead
                    title={job.name || job.id}
                    pills={<PanelPill tone={stateTone(job)}>{job.state}</PanelPill>}
                    actions={
                      <>
                        <Button
                          variant="ghost"
                          size="sm"
                          icon={
                            paused ? (
                              <Play aria-hidden="true" size={13} />
                            ) : (
                              <Pause aria-hidden="true" size={13} />
                            )
                          }
                          onClick={() => void act(job, paused ? "resume" : "pause")}
                        >
                          {paused ? t("manage.cron.resume") : t("manage.cron.pause")}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          icon={<Zap aria-hidden="true" size={13} />}
                          onClick={() => void act(job, "trigger")}
                        >
                          {t("manage.cron.runNow")}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          icon={<Pencil aria-hidden="true" size={13} />}
                          onClick={() => setDialog({ kind: "edit", job })}
                        >
                          {t("manage.cron.edit")}
                        </Button>
                      </>
                    }
                  />
                  <MetaGrid
                    rows={[
                      [t("cron.frequency"), job.schedule_display || job.schedule],
                      [t("cron.nextRun"), when(job.next_run_at)],
                      [t("manage.cron.lastRun"), when(job.last_run_at)],
                      [t("manage.cron.deliver"), job.deliver || "—"],
                      ...(all ? [[t("manage.cron.profile"), job.profile] as const] : []),
                      ...(job.skills.length > 0
                        ? [[t("cron.skills"), job.skills.join(", ")] as const]
                        : []),
                    ]}
                  />
                  <section className="cron-section">
                    <CapsLabel>{t("manage.cron.prompt")}</CapsLabel>
                    <PanelBlock>{job.prompt}</PanelBlock>
                  </section>
                  <section className="cron-section">
                    <CapsLabel>{t("cron.runs")}</CapsLabel>
                    {job.last_run_at === null ? (
                      <p className="cron-note">{t("cron.noRuns")}</p>
                    ) : (
                      <div className="cron-run">
                        <span>{when(job.last_run_at)}</span>
                        {job.last_status !== null && (
                          <PanelPill tone={job.last_error === null ? "good" : "bad"}>
                            {job.last_status}
                          </PanelPill>
                        )}
                        {job.last_error !== null && (
                          <span className="cron-run-error">{job.last_error}</span>
                        )}
                      </div>
                    )}
                  </section>
                </div>
              ),
            })}
        empty={{
          icon: <Watch aria-hidden="true" />,
          title: t("cron.none"),
          description: t("cron.emptyBody"),
          action: newButton,
        }}
      />
    );
  }

  return (
    <OverlayCard
      id="cron"
      title={t("nav.cron")}
      closeLabel={t("common.close")}
      onClose={props.onClose}
    >
      {body}
      {dialog !== null && (
        <CronDialog
          api={api}
          profile={own}
          {...(dialog.kind === "edit" ? { job: dialog.job } : {})}
          {...(dialog.kind === "new" && dialog.blueprint !== undefined
            ? { blueprint: dialog.blueprint }
            : {})}
          blueprints={plans}
          targets={targets.state === "ok" ? targets.data.targets : []}
          onDone={() => {
            setDialog(null);
            jobs.reload();
          }}
          onClose={() => setDialog(null)}
        />
      )}
    </OverlayCard>
  );
}
