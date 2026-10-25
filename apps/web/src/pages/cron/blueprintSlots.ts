// A blueprint's prompt slots as form inputs (W56-G2-3). `GET /api/cron/blueprints` sends each
// blueprint's prompt already filled from its slot defaults, with a slot that has no default left as
// `{name}`, plus the slots themselves (`fields`). The form turns that back into a template it can
// fill again: a `{name}` still in the prompt is the slot's place, and a defaulted slot's place is
// where its default appears, when it appears exactly once. A slot whose place cannot be found that
// way gets no input; the person edits the prompt text instead.
//
// Only `text` and `choice` slots become inputs here: `time`, `weekdays` and `interval` slots set
// the schedule, which the schedule picker already edits.

import type { CronBlueprint, CronBlueprintField } from "../../lib/restTypes.ts";

export interface SlotPlan {
  /** The prompt with `{name}` at each placed slot. */
  template: string;
  /** The slots that got an input, in the blueprint's order. */
  slots: CronBlueprintField[];
}

const PROMPT_KINDS: ReadonlySet<CronBlueprintField["kind"]> = new Set(["text", "choice"]);

function occurrences(text: string, part: string): number[] {
  const found: number[] = [];
  if (!part) return found;
  for (let at = text.indexOf(part); at !== -1; at = text.indexOf(part, at + 1)) found.push(at);
  return found;
}

export function planSlots(blueprint: Pick<CronBlueprint, "prompt" | "fields">): SlotPlan {
  const prompt = blueprint.prompt ?? "";
  const spans: { start: number; end: number; field: CronBlueprintField }[] = [];
  const placed = new Set<string>();
  for (const field of blueprint.fields) {
    if (!PROMPT_KINDS.has(field.kind)) continue;
    const token = `{${field.name}}`;
    const open = occurrences(prompt, token);
    const fromDefault =
      open.length === 0 && field.default ? occurrences(prompt, field.default) : [];
    const hits = open.length > 0 ? open : fromDefault.length === 1 ? fromDefault : [];
    const length = open.length > 0 ? token.length : (field.default ?? "").length;
    const fresh = hits
      .map((start) => ({ start, end: start + length, field }))
      .filter((span) => !spans.some((s) => span.start < s.end && s.start < span.end));
    if (fresh.length === 0 || fresh.length !== hits.length) continue;
    spans.push(...fresh);
    placed.add(field.name);
  }
  spans.sort((a, b) => a.start - b.start);
  let template = "";
  let at = 0;
  for (const span of spans) {
    template += `${prompt.slice(at, span.start)}{${span.field.name}}`;
    at = span.end;
  }
  template += prompt.slice(at);
  return { template, slots: blueprint.fields.filter((f) => placed.has(f.name)) };
}

/** Each slot's starting value: its default, else empty (a choice falls back to its first option). */
export function startValues(slots: readonly CronBlueprintField[]): Record<string, string> {
  return Object.fromEntries(
    slots.map((f) => [f.name, f.default ?? (f.kind === "choice" ? (f.options?.[0] ?? "") : "")]),
  );
}

/**
 * The prompt with the slots' values in place. An optional slot left empty drops out; a required one
 * left empty stays `{name}` (`missingSlots` names it). Names that are not slots stay as written.
 */
export function fillTemplate(
  template: string,
  slots: readonly CronBlueprintField[],
  values: Readonly<Record<string, string>>,
): string {
  const byName = new Map(slots.map((f) => [f.name, f]));
  return template
    .replace(/\{([^{}\s]+)\}/g, (token, name: string) => {
      const field = byName.get(name);
      if (!field) return token;
      const value = (values[name] ?? "").trim();
      return value || (field.optional ? "" : token);
    })
    .trim();
}

/** Labels of the required slots still empty. */
export function missingSlots(
  slots: readonly CronBlueprintField[],
  values: Readonly<Record<string, string>>,
): string[] {
  return slots.filter((f) => !f.optional && !(values[f.name] ?? "").trim()).map((f) => f.label);
}

/** Slot names of `fields` still written as `{name}` in a prompt (any kind). */
export function openPlaceholders(prompt: string, fields: readonly CronBlueprintField[]): string[] {
  return fields.filter((f) => prompt.includes(`{${f.name}}`)).map((f) => f.name);
}
