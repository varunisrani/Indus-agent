// The schedule picker of the Cron job panel (W52-c): a mode plus a few fields → one string in our
// cron grammar (P14-01), and back again when a job is edited. The server still validates whatever
// is sent; a schedule the picker cannot read back opens as "Custom" with its raw text.

export type ScheduleMode = "interval" | "daily" | "weekly" | "monthly" | "once" | "custom";
export type IntervalUnit = "m" | "h" | "d";

export interface ScheduleDraft {
  mode: ScheduleMode;
  /** interval: how many units between runs. */
  every: number;
  unit: IntervalUnit;
  /** daily / weekly / monthly: `HH:MM`, 24-hour. */
  time: string;
  /** weekly: days of the week, 0 = Sunday … 6 = Saturday. */
  days: number[];
  /** monthly: 1–28 (every month has these days). */
  dayOfMonth: number;
  /** once: a local date-time `YYYY-MM-DDTHH:MM` (the agent's zone, as the grammar reads it). */
  at: string;
  /** custom: any text in our grammar. */
  custom: string;
}

export const SCHEDULE_MODES: ReadonlyArray<{ value: ScheduleMode; label: string }> = [
  { value: "interval", label: "Every interval" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "once", label: "Once" },
  { value: "custom", label: "Custom" },
];

export const INTERVAL_UNITS: ReadonlyArray<{ value: IntervalUnit; label: string }> = [
  { value: "m", label: "minutes" },
  { value: "h", label: "hours" },
  { value: "d", label: "days" },
];

/** Short day names in cron order (0 = Sunday); the picker shows them Monday first. */
export const WEEK_DAYS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 1, label: "Mon" },
  { value: 2, label: "Tue" },
  { value: 3, label: "Wed" },
  { value: 4, label: "Thu" },
  { value: 5, label: "Fri" },
  { value: 6, label: "Sat" },
  { value: 0, label: "Sun" },
];

export const DEFAULT_DRAFT: ScheduleDraft = {
  mode: "interval",
  every: 30,
  unit: "m",
  time: "09:00",
  days: [1, 2, 3, 4, 5],
  dayOfMonth: 1,
  at: "",
  custom: "",
};

const CLOCK = /^([01]?\d|2[0-3]):([0-5]\d)$/;
const LOCAL_AT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

function clockParts(time: string): [number, number] {
  const match = CLOCK.exec(time);
  return [Number(match?.[1] ?? 0), Number(match?.[2] ?? 0)];
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Why the draft cannot become a schedule, or null when it can. */
export function draftProblem(draft: ScheduleDraft): string | null {
  switch (draft.mode) {
    case "interval":
      return Number.isInteger(draft.every) && draft.every >= 1
        ? null
        : "Use a whole number of 1 or more.";
    case "daily":
      return CLOCK.test(draft.time) ? null : "Pick a time of day.";
    case "weekly":
      if (draft.days.length === 0) return "Pick at least one day.";
      return CLOCK.test(draft.time) ? null : "Pick a time of day.";
    case "monthly":
      if (!Number.isInteger(draft.dayOfMonth) || draft.dayOfMonth < 1 || draft.dayOfMonth > 28)
        return "Use a day from 1 to 28.";
      return CLOCK.test(draft.time) ? null : "Pick a time of day.";
    case "once":
      return LOCAL_AT.test(draft.at) ? null : "Pick a date and time.";
    case "custom":
      return draft.custom.trim() ? null : "Write a schedule.";
  }
}

/** The schedule text for a valid draft (`every 30m`, `daily at 09:00`, a cron line, …). */
export function draftToSchedule(draft: ScheduleDraft): string {
  switch (draft.mode) {
    case "interval":
      return `every ${draft.every}${draft.unit}`;
    case "daily":
      return `daily at ${draft.time.padStart(5, "0")}`;
    case "weekly": {
      const [hour, minute] = clockParts(draft.time);
      const days = [...new Set(draft.days)].sort((a, b) => a - b).join(",");
      return `${minute} ${hour} * * ${days}`;
    }
    case "monthly": {
      const [hour, minute] = clockParts(draft.time);
      return `${minute} ${hour} ${draft.dayOfMonth} * *`;
    }
    case "once":
      return draft.at;
    case "custom":
      return draft.custom.trim().replace(/\s+/g, " ");
  }
}

/** Read a stored schedule back into the picker; anything unfamiliar becomes Custom. */
export function scheduleToDraft(schedule: string): ScheduleDraft {
  const text = schedule.trim();
  const lower = text.toLowerCase();
  const base = { ...DEFAULT_DRAFT, custom: text };
  const every = /^every (\d+)\s*([mhd])$/.exec(lower);
  if (every)
    return { ...base, mode: "interval", every: Number(every[1]), unit: every[2] as IntervalUnit };
  const daily = /^(?:daily|every day) at (\d{1,2}:\d{2})$/.exec(lower);
  if (daily?.[1] && CLOCK.test(daily[1]))
    return { ...base, mode: "daily", time: daily[1].padStart(5, "0") };
  const cron = /^(\d{1,2}) (\d{1,2}) (\S+) \* (\S+)$/.exec(lower);
  if (cron) {
    const minute = Number(cron[1]);
    const hour = Number(cron[2]);
    if (minute < 60 && hour < 24) {
      const time = `${pad(hour)}:${pad(minute)}`;
      const dom = cron[3] ?? "";
      const dow = cron[4] ?? "";
      if (dom === "*" && /^[0-6](,[0-6])*$/.test(dow))
        return { ...base, mode: "weekly", time, days: dow.split(",").map(Number) };
      if (dow === "*" && /^\d{1,2}$/.test(dom) && Number(dom) >= 1 && Number(dom) <= 28)
        return { ...base, mode: "monthly", time, dayOfMonth: Number(dom) };
    }
  }
  if (/^\d{4}-\d{2}-\d{2}t\d{2}:\d{2}$/.test(lower)) return { ...base, mode: "once", at: text };
  return { ...base, mode: "custom" };
}
