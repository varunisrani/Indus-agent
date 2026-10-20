// The schedule helper on the Cron page: turns a few choices into a string in our cron grammar
// (P14-01) so nobody has to remember it. The server still validates whatever is sent.

export type BuilderKind = "every" | "in" | "daily" | "weekdays" | "cron";
export type BuilderUnit = "m" | "h" | "d";

export interface BuilderChoice {
  kind: BuilderKind;
  amount: number;
  unit: BuilderUnit;
  /** `HH:MM`, 24-hour. */
  time: string;
  cron: string;
}

export const BUILDER_KINDS: ReadonlyArray<{ value: BuilderKind; label: string }> = [
  { value: "every", label: "Repeat every…" },
  { value: "in", label: "Once, after…" },
  { value: "daily", label: "Every day at…" },
  { value: "weekdays", label: "Weekdays at…" },
  { value: "cron", label: "Cron expression" },
];

export const BUILDER_UNITS: ReadonlyArray<{ value: BuilderUnit; label: string }> = [
  { value: "m", label: "minutes" },
  { value: "h", label: "hours" },
  { value: "d", label: "days" },
];

export const DEFAULT_CHOICE: BuilderChoice = {
  kind: "every",
  amount: 30,
  unit: "m",
  time: "09:00",
  cron: "0 9 * * 1-5",
};

const TIME = /^([01]?\d|2[0-3]):([0-5]\d)$/;
const CRON_FIELD = /^[A-Za-z0-9*,\-/]+$/;

/** Why the choice cannot become a schedule, or null when it can. */
export function builderProblem(choice: BuilderChoice): string | null {
  if (choice.kind === "every" || choice.kind === "in") {
    if (!Number.isInteger(choice.amount) || choice.amount < 1)
      return "Use a whole number of 1 or more.";
    return null;
  }
  if (choice.kind === "daily" || choice.kind === "weekdays") {
    return TIME.test(choice.time) ? null : "Use a 24-hour time such as 09:30.";
  }
  const fields = choice.cron.trim().split(/\s+/);
  return fields.length === 5 && fields.every((f) => CRON_FIELD.test(f))
    ? null
    : "A cron expression has exactly five fields.";
}

/** The schedule text for a valid choice (`every 30m`, `in 2h`, `daily at 09:00`, …). */
export function buildSchedule(choice: BuilderChoice): string {
  switch (choice.kind) {
    case "every":
      return `every ${choice.amount}${choice.unit}`;
    case "in":
      return `in ${choice.amount}${choice.unit}`;
    case "daily":
      return `daily at ${choice.time}`;
    case "weekdays":
      return `weekdays at ${choice.time}`;
    case "cron":
      return choice.cron.trim().split(/\s+/).join(" ");
  }
}
