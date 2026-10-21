import { describe, expect, it } from "vitest";
import { CRASH_RELOAD_WINDOW_MS, CrashReloadBudget, isIntentionalExit } from "./crashReload.ts";

describe("renderer crash reloads", () => {
  it("the 4th crash in 60 s does not reload (fake clock)", () => {
    let now = 0;
    const budget = new CrashReloadBudget(() => now);
    const answers: boolean[] = [];
    for (let i = 0; i < 4; i += 1) {
      answers.push(budget.allow());
      now += 10_000;
    }
    expect(answers).toEqual([true, true, true, false]);
  });

  it("reloads again once the oldest reload leaves the window", () => {
    let now = 0;
    const budget = new CrashReloadBudget(() => now);
    budget.allow();
    now = 1;
    budget.allow();
    now = 2;
    budget.allow();
    now = CRASH_RELOAD_WINDOW_MS - 1;
    expect(budget.allow()).toBe(false);
    now = CRASH_RELOAD_WINDOW_MS;
    expect(budget.allow()).toBe(true);
  });

  it("a clean exit is not a crash", () => {
    expect(isIntentionalExit("clean-exit")).toBe(true);
    expect(isIntentionalExit("crashed")).toBe(false);
    expect(isIntentionalExit("oom")).toBe(false);
  });
});
