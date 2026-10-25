// The new / edit cron job dialog (W52-l, spec 3.7): start from a blueprint, name, prompt, a
// frequency preset (or a custom expression — our free-text schedules such as "every 2h" stay valid
// there), the delivery targets as checkboxes, and a model select that stays disabled (our jobs have
// no per-job model). The body is exactly what `POST/PUT /api/cron/jobs` accepts.

import { useState } from "react";
import { useT } from "../../i18n/index.tsx";
import { type RestLike, rest } from "../../settings/settingsApi.ts";
import { Button, SmallDialog } from "../../ui/components.tsx";
import { Field, InlineError } from "../kit.tsx";
import type { DeliveryTarget } from "../webhooks/WebhooksPage.tsx";
import type { Blueprint, OwnedJob } from "./CronOverlay.tsx";

export const FREQUENCIES = [
  ["daily", "0 9 * * *"],
  ["weekdays", "0 9 * * 1-5"],
  ["weekly", "0 9 * * 1"],
  ["monthly", "0 9 1 * *"],
  ["hourly", "0 * * * *"],
  ["every15", "*/15 * * * *"],
] as const;
export type Frequency = (typeof FREQUENCIES)[number][0] | "custom";

/** The preset an expression is, or "custom". */
export function presetOf(schedule: string): Frequency {
  const found = FREQUENCIES.find(([, expr]) => expr === schedule.trim());
  return found === undefined ? "custom" : found[0];
}

export function expressionOf(frequency: Frequency, custom: string): string {
  if (frequency === "custom") return custom.trim();
  return FREQUENCIES.find(([id]) => id === frequency)?.[1] ?? custom.trim();
}

/** Comma-joined target ids (the backend splits on commas). */
export function deliverList(value: string | null | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

export function CronDialog(props: {
  api: RestLike | null;
  profile: string;
  job?: OwnedJob;
  blueprints: readonly Blueprint[];
  targets: readonly DeliveryTarget[];
  /** Pre-select a blueprint (a click on a blueprint row). */
  blueprint?: Blueprint;
  onDone: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const start = props.blueprint;
  const [planId, setPlanId] = useState(start?.skill ?? "");
  const [name, setName] = useState(props.job?.name ?? start?.skill ?? "");
  const [prompt, setPrompt] = useState(
    props.job?.prompt ?? (start === undefined ? "" : (start.prompt ?? start.description)),
  );
  const initialSchedule = props.job?.schedule ?? start?.schedule ?? FREQUENCIES[0][1];
  const [frequency, setFrequency] = useState<Frequency>(presetOf(initialSchedule));
  const [custom, setCustom] = useState(
    presetOf(initialSchedule) === "custom" ? initialSchedule : "",
  );
  const [deliver, setDeliver] = useState<string[]>(deliverList(props.job?.deliver));
  const [skills, setSkills] = useState<string[]>(
    props.job?.skills ?? (start === undefined ? [] : [start.skill]),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const editing = props.job !== undefined;
  const plan = props.blueprints.find((b) => b.skill === planId);
  const pickPlan = (id: string) => {
    setPlanId(id);
    const next = props.blueprints.find((b) => b.skill === id);
    if (next === undefined) {
      setSkills([]);
      return;
    }
    setName(next.skill);
    setPrompt(next.prompt ?? next.description);
    const schedule = next.schedule ?? FREQUENCIES[0][1];
    setFrequency(presetOf(schedule));
    if (presetOf(schedule) === "custom") setCustom(schedule);
    setSkills([next.skill]);
  };
  const schedule = expressionOf(frequency, custom);
  const submit = async () => {
    setBusy(true);
    setError("");
    const body = {
      ...(name.trim() === "" ? {} : { name: name.trim() }),
      prompt,
      schedule,
      ...(deliver.length === 0 ? {} : { deliver: deliver.join(",") }),
      ...(skills.length > 0 ? { skills } : {}),
    };
    const answer =
      props.job === undefined
        ? await rest(props.api, "POST", "/api/cron/jobs", { profile: props.profile, body })
        : await rest(props.api, "PUT", `/api/cron/jobs/${encodeURIComponent(props.job.id)}`, {
            profile: props.job.profile,
            body,
          });
    setBusy(false);
    if (!answer.ok) {
      setError(answer.message);
      return;
    }
    props.onDone();
  };
  return (
    <SmallDialog
      size="wide"
      title={editing ? t("cron.editTitle") : t("cron.newTitle")}
      description={t("cron.newBody")}
      closeLabel={t("common.close")}
      onClose={props.onClose}
      actions={
        <>
          <Button variant="outline" onClick={props.onClose}>
            {t("manage.cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={busy || prompt.trim() === "" || schedule === ""}
            onClick={() => void submit()}
          >
            {busy ? t("cron.saving") : editing ? t("cron.saveChanges") : t("cron.create")}
          </Button>
        </>
      }
    >
      {!editing && props.blueprints.length > 0 && (
        <Field
          label={t("manage.cron.blueprint")}
          htmlFor="cron-plan"
          {...(plan === undefined ? {} : { help: plan.description })}
        >
          <select id="cron-plan" value={planId} onChange={(e) => pickPlan(e.target.value)}>
            <option value="">{t("cron.custom")}</option>
            {props.blueprints.map((b) => (
              <option key={b.skill} value={b.skill}>
                {b.skill}
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field label={t("manage.cron.name")} hint={t("cron.optional")} htmlFor="cron-name">
        <input
          id="cron-name"
          value={name}
          placeholder={t("cron.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <Field label={t("manage.cron.prompt")} htmlFor="cron-prompt">
        <textarea
          id="cron-prompt"
          className="kit-mono"
          value={prompt}
          placeholder={t("cron.promptPlaceholder")}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </Field>
      <div className="kit-two-cols">
        <Field label={t("cron.frequency")} htmlFor="cron-frequency">
          <select
            id="cron-frequency"
            value={frequency}
            onChange={(e) => setFrequency(e.target.value as Frequency)}
          >
            {FREQUENCIES.map(([id]) => (
              <option key={id} value={id}>
                {t(`cron.freq.${id}`)}
              </option>
            ))}
            <option value="custom">{t("cron.custom")}</option>
          </select>
        </Field>
        <fieldset className="kit-field cron-targets">
          <legend className="kit-field-label">{t("manage.cron.deliver")}</legend>
          <div className="cron-target-box">
            {props.targets.map((target) => (
              <label key={target.id} className="cron-target" data-off={!target.available}>
                <input
                  type="checkbox"
                  checked={deliver.includes(target.id)}
                  disabled={!target.available}
                  onChange={(e) =>
                    setDeliver(
                      e.target.checked
                        ? [...deliver, target.id]
                        : deliver.filter((id) => id !== target.id),
                    )
                  }
                />
                <span>
                  {target.kind === "local" ? t("cron.thisComputer") : target.label}
                  {target.available ? "" : ` ${t("cron.needsHome")}`}
                </span>
              </label>
            ))}
            {props.targets.length === 0 && (
              <span className="kit-field-help">{t("cron.noTargets")}</span>
            )}
          </div>
        </fieldset>
      </div>
      <Field label={t("cron.model")} hint={t("cron.optional")} htmlFor="cron-model">
        <select id="cron-model" disabled title={t("cc.notYet")}>
          <option>{t("cron.defaultModel")}</option>
        </select>
      </Field>
      {frequency === "custom" ? (
        <Field label={t("cron.expression")} help={t("cron.expressionHelp")} htmlFor="cron-expr">
          <input
            id="cron-expr"
            className="kit-mono"
            value={custom}
            placeholder="every 2h"
            onChange={(e) => setCustom(e.target.value)}
          />
        </Field>
      ) : (
        <div className="cron-summary">
          <span>{t(`cron.says.${frequency}`)}</span>
          <code>{schedule}</code>
        </div>
      )}
      {error !== "" && <InlineError>{error}</InlineError>}
    </SmallDialog>
  );
}
