import { describe, expect, it } from "vitest";
import type { CronBlueprintField } from "../../lib/restTypes.ts";
import {
  fillTemplate,
  missingSlots,
  openPlaceholders,
  planSlots,
  startValues,
} from "./blueprintSlots.ts";

function slot(field: Partial<CronBlueprintField> & Pick<CronBlueprintField, "name" | "kind">) {
  return { label: field.name, default: null, optional: false, help: "", ...field };
}

const time = slot({ name: "time", kind: "time", default: "16:30" });
const style = slot({ name: "style", kind: "choice", default: "brief", options: ["brief", "full"] });
const target = slot({ name: "target", kind: "text", label: "What to check", max: 200 });
const extra = slot({ name: "extra", kind: "text", optional: true });

describe("blueprint prompt slots", () => {
  it("places a defaulted slot where its default appears once, and keeps timing slots out", () => {
    const plan = planSlots({ prompt: "Review the week. Style: brief.", fields: [time, style] });
    expect(plan.template).toBe("Review the week. Style: {style}.");
    expect(plan.slots.map((f) => f.name)).toEqual(["style"]);
    expect(fillTemplate(plan.template, plan.slots, { style: "full" })).toBe(
      "Review the week. Style: full.",
    );
  });

  it("uses a slot left as {name} by the server as its place", () => {
    const plan = planSlots({ prompt: "Check {target}. Report changes.", fields: [target] });
    expect(plan.template).toBe("Check {target}. Report changes.");
    const values = startValues(plan.slots);
    expect(values).toEqual({ target: "" });
    expect(missingSlots(plan.slots, values)).toEqual(["What to check"]);
    expect(fillTemplate(plan.template, plan.slots, values)).toBe("Check {target}. Report changes.");
    expect(fillTemplate(plan.template, plan.slots, { target: " the build page " })).toBe(
      "Check the build page. Report changes.",
    );
  });

  it("gives no input to a slot whose place is ambiguous or gone", () => {
    // "brief" twice: which one is the slot cannot be told.
    expect(planSlots({ prompt: "A brief note. Style: brief.", fields: [style] }).slots).toEqual([]);
    // An optional slot with no default was filled with nothing: no trace left in the prompt.
    expect(planSlots({ prompt: "Wrap up my day.", fields: [extra] }).slots).toEqual([]);
    expect(planSlots({ prompt: null, fields: [style] })).toEqual({ template: "", slots: [] });
  });

  it("drops an optional slot left empty and never re-reads a value as a placeholder", () => {
    const plan = planSlots({ prompt: "Wrap up. {extra}", fields: [extra] });
    expect(fillTemplate(plan.template, plan.slots, { extra: "" })).toBe("Wrap up.");
    expect(fillTemplate(plan.template, plan.slots, { extra: "{extra} stays" })).toBe(
      "Wrap up. {extra} stays",
    );
    expect(missingSlots(plan.slots, { extra: "" })).toEqual([]);
  });

  it("starts a choice without a default on its first option", () => {
    const pick = slot({ name: "pick", kind: "choice", options: ["a", "b"] });
    expect(startValues([pick])).toEqual({ pick: "a" });
  });

  it("names the slots still written as {name} in a prompt", () => {
    expect(openPlaceholders("Check {target} and {other}.", [target, style])).toEqual(["target"]);
  });
});
