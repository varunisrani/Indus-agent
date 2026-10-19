import { afterEach, describe, expect, it } from "vitest";
import { uiStore } from "../state/uiStore.ts";
import {
  allHidden,
  applyDetailsCommand,
  type DetailsState,
  detailsCommand,
  detailsStore,
  nextMode,
  readOverrides,
  resolveSections,
  SECTION_DEFAULTS,
  sectionMode,
} from "./details.ts";

const base: DetailsState = { global: "collapsed", overrides: {}, globalFromCommand: false };

afterEach(() => {
  detailsStore.getState().set({ overrides: {}, globalFromCommand: false });
  uiStore.getState().patch({ detailsMode: "collapsed" });
});

describe("section resolution", () => {
  it("starts from the built-in defaults over the global mode", () => {
    expect(SECTION_DEFAULTS).toEqual({
      thinking: "expanded",
      tools: "expanded",
      activity: "hidden",
    });
    expect(resolveSections("collapsed")).toEqual({
      thinking: "expanded",
      tools: "expanded",
      subagents: "collapsed",
      activity: "hidden",
    });
  });

  it("lets a per-section override win over the global mode", () => {
    expect(sectionMode("tools", "hidden", { tools: "expanded" })).toBe("expanded");
    expect(sectionMode("tools", "expanded", { tools: "hidden" }, true)).toBe("hidden");
    expect(sectionMode("activity", "expanded", { activity: "collapsed" }, true)).toBe("collapsed");
  });

  it("applies a /details global mode to every section without an override", () => {
    expect(resolveSections("hidden", {}, true)).toEqual({
      thinking: "hidden",
      tools: "hidden",
      subagents: "hidden",
      activity: "hidden",
    });
    expect(allHidden(resolveSections("hidden", {}, true))).toBe(true);
    expect(allHidden(resolveSections("hidden", { activity: "collapsed" }, true))).toBe(false);
  });

  it("reads config overrides, dropping unknown names and modes", () => {
    expect(readOverrides({ tools: "Hidden", bogus: "expanded", thinking: "loud" })).toEqual({
      tools: "hidden",
    });
    expect(readOverrides(["tools"])).toEqual({});
    expect(readOverrides(null)).toEqual({});
  });

  it("cycles hidden → collapsed → expanded", () => {
    expect(nextMode("hidden")).toBe("collapsed");
    expect(nextMode("collapsed")).toBe("expanded");
    expect(nextMode("expanded")).toBe("hidden");
  });
});

describe("/details", () => {
  it("reports the current modes with no arguments", () => {
    const result = detailsCommand(base, "");
    expect(result).toMatchObject({
      ok: true,
      message: expect.stringContaining("thinking expanded"),
    });
  });

  it("sets or cycles the global mode", () => {
    const set = detailsCommand(base, "Hidden");
    expect(set.ok && set.state).toEqual({
      global: "hidden",
      overrides: {},
      globalFromCommand: true,
    });
    const cycled = detailsCommand(base, "cycle");
    expect(cycled.ok && cycled.state.global).toBe("expanded");
  });

  it("sets, cycles and resets one section", () => {
    const set = detailsCommand(base, "tools collapsed");
    expect(set.ok && set.state.overrides).toEqual({ tools: "collapsed" });
    const cycled = detailsCommand(base, "thinking cycle");
    expect(cycled.ok && cycled.state.overrides).toEqual({ thinking: "hidden" });
    const reset = detailsCommand(
      { ...base, overrides: { tools: "hidden", activity: "expanded" } },
      "tools reset",
    );
    expect(reset.ok && reset.state.overrides).toEqual({ activity: "expanded" });
  });

  it("rejects bad modes and sections without changing anything", () => {
    expect(detailsCommand(base, "loud").ok).toBe(false);
    expect(detailsCommand(base, "nope hidden").ok).toBe(false);
    expect(detailsCommand(base, "tools loud").ok).toBe(false);
    expect(detailsCommand(base, "tools hidden extra").ok).toBe(false);
  });

  it("writes through to the stores", () => {
    expect(applyDetailsCommand("expanded").ok).toBe(true);
    expect(uiStore.getState().detailsMode).toBe("expanded");
    expect(detailsStore.getState().globalFromCommand).toBe(true);
    applyDetailsCommand("activity collapsed");
    expect(detailsStore.getState().overrides).toEqual({ activity: "collapsed" });
    expect(applyDetailsCommand("bogus").ok).toBe(false);
    expect(uiStore.getState().detailsMode).toBe("expanded");
  });
});
