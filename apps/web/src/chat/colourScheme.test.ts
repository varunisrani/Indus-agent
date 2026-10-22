import { describe, expect, it } from "vitest";
import { SchemeReporter, schemeReport } from "./colourScheme.ts";

function rig(light = true) {
  const state = { light };
  const sent: string[] = [];
  const reporter = new SchemeReporter(
    () => state.light,
    (data) => sent.push(data),
  );
  return { state, sent, reporter };
}

describe("colour-scheme reports (W47 d)", () => {
  it("answers the scheme query with the dashboard's current scheme", () => {
    const { state, sent, reporter } = rig(true);
    expect(reporter.deviceStatus([996])).toBe(true);
    state.light = false;
    expect(reporter.deviceStatus([996])).toBe(true);
    expect(sent).toEqual(["\u001b[?997;2n", "\u001b[?997;1n"]);
    // other DSRs are the terminal's own business
    expect(reporter.deviceStatus([6])).toBe(false);
    expect(sent).toHaveLength(2);
  });

  it("reports a theme switch only after mode 2031 is set, and only a real change", () => {
    const { state, sent, reporter } = rig(true);
    state.light = false;
    reporter.themeChanged();
    expect(sent).toEqual([]);
    expect(reporter.setMode([1004, 2031], true)).toBe(false); // other modes still apply
    expect(reporter.notifying).toBe(true);
    reporter.themeChanged();
    reporter.themeChanged(); // the same scheme again: no second report
    state.light = true;
    reporter.themeChanged();
    expect(sent).toEqual([schemeReport(false), schemeReport(true)]);
    reporter.setMode([2031], false);
    state.light = false;
    reporter.themeChanged();
    expect(sent).toHaveLength(2);
  });

  it("forgets the mode when a new program starts", () => {
    const { state, sent, reporter } = rig(false);
    reporter.setMode([2031], true);
    reporter.reset();
    state.light = true;
    reporter.themeChanged();
    expect(sent).toEqual([]);
  });
});
