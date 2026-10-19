import type { Clock } from "@indus/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { uiStore } from "../state/uiStore.ts";
import {
  ConfigSync,
  displaySettings,
  normalizeBusy,
  normalizeMouse,
  normalizeStatusBar,
  POLL_MS,
} from "./configSync.ts";

/** Timers that run only when the test moves time forward. */
class ManualClock implements Clock {
  private time = 0;
  private timers: { at: number; run: () => void; id: number }[] = [];
  private next = 0;
  now(): number {
    return this.time;
  }
  setTimeout(run: () => void, ms: number): unknown {
    this.next += 1;
    this.timers.push({ at: this.time + ms, run, id: this.next });
    return this.next;
  }
  clearTimeout(handle: unknown): void {
    this.timers = this.timers.filter((t) => t.id !== handle);
  }
  get pending(): number {
    return this.timers.length;
  }
  /** Move time on, running due timers and letting their promises settle. */
  async advance(ms: number): Promise<void> {
    const until = this.time + ms;
    for (;;) {
      const due = this.timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at)[0];
      if (due === undefined) break;
      this.timers = this.timers.filter((t) => t !== due);
      this.time = due.at;
      due.run();
      await settle();
    }
    this.time = until;
  }
}

const settle = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

/** A bridge stand-in for `config.get` / `reload.mcp` with switchable failures. */
function fakeBridge(initial: Record<string, unknown>) {
  const state = {
    full: { ...initial },
    mtime: 100 as number | null,
    failMtime: false,
    failFull: false,
    failReload: false,
  };
  const calls: [string, unknown][] = [];
  const call = vi.fn(async (method: string, params: unknown) => {
    calls.push([method, params]);
    const key = (params as { key?: string }).key;
    if (method === "config.get" && key === "mtime") {
      if (state.failMtime) throw new Error("bridge busy");
      return { key, value: state.mtime };
    }
    if (method === "config.get" && key === "full") {
      if (state.failFull) throw new Error("bridge busy");
      return { key, value: { ...state.full } };
    }
    if (method === "reload.mcp") {
      if (state.failReload) throw new Error("reload failed");
      return { reloaded: true, message: "MCP servers reconnected." };
    }
    throw new Error(`unexpected ${method}`);
  });
  return { state, calls, rpc: { call } };
}

beforeEach(() => {
  uiStore.setState({
    sid: "s-1",
    storedSid: "s-1",
    revision: 0,
    busyInputMode: "interrupt",
    detailsMode: "collapsed",
    statusBar: "top",
    indicatorStyle: "spinner",
  });
});

describe("normalisers", () => {
  it("busy: a missing or unknown value means queue in the TUI (R05.18)", () => {
    expect(normalizeBusy(undefined)).toBe("queue");
    expect(normalizeBusy(null)).toBe("queue");
    expect(normalizeBusy("sideways")).toBe("queue");
    expect(normalizeBusy(" Steer ")).toBe("steer");
    expect(normalizeBusy("interrupt")).toBe("interrupt");
  });

  it("status bar: booleans and positions", () => {
    expect(normalizeStatusBar(false)).toBe("off");
    expect(normalizeStatusBar(true)).toBe("top");
    expect(normalizeStatusBar(undefined)).toBe("top");
    expect(normalizeStatusBar("on")).toBe("top");
    expect(normalizeStatusBar("bottom")).toBe("bottom");
    expect(normalizeStatusBar("off")).toBe("off");
  });

  it("mouse: booleans and presets", () => {
    expect(normalizeMouse(true)).toBe("all");
    expect(normalizeMouse(false)).toBe("off");
    expect(normalizeMouse(undefined)).toBe("off");
    expect(normalizeMouse("wheel")).toBe("wheel");
    expect(normalizeMouse("buttons")).toBe("buttons");
    expect(normalizeMouse("on")).toBe("all");
  });

  it("maps the bridge's flat keys and fills defaults", () => {
    const { patch, extras } = displaySettings({
      busy: "steer",
      show_reasoning: true,
      statusbar: false,
      indicator: "dots",
      details: "expanded",
      mouse: true,
      theme: "Dusk",
      bell_on_prompt: false,
      paste_lines: "8",
      status_bar_fields: ["Model", " cost "],
    });
    expect(patch).toMatchObject({
      busyInputMode: "steer",
      showReasoning: true,
      statusBar: "off",
      indicatorStyle: "dots",
      detailsMode: "expanded",
      mousePreset: "all",
      themeName: "dusk",
      streaming: true,
      inlineDiffs: true,
      timestamps: false,
      pasteLines: 8,
      pasteChars: 2000,
    });
    expect(extras).toMatchObject({
      bellOnPrompt: false,
      statusBarFields: ["model", "cost"],
      mcpRevision: null,
    });
  });

  it("a missing busy key gives queue", () => {
    expect(displaySettings({ indicator: "none" }).patch.busyInputMode).toBe("queue");
  });
});

describe("sync", () => {
  it("reads full once on start, even if started twice", async () => {
    const bridge = fakeBridge({ busy: "steer" });
    const sync = new ConfigSync({ rpc: bridge.rpc, clock: new ManualClock() });
    await Promise.all([sync.start(), sync.start()]);
    const fulls = bridge.calls.filter(([, p]) => (p as { key: string }).key === "full");
    expect(fulls).toEqual([["config.get", { key: "full", session_id: "s-1" }]]);
    expect(uiStore.getState().busyInputMode).toBe("steer");
    sync.stop();
  });

  it("re-applies when the stamp changes at the 5 s poll", async () => {
    const clock = new ManualClock();
    const bridge = fakeBridge({ busy: "steer", details: "collapsed" });
    const sync = new ConfigSync({ rpc: bridge.rpc, clock });
    await sync.start();
    bridge.state.full = { busy: "interrupt", details: "expanded" };
    await clock.advance(POLL_MS);
    expect(uiStore.getState().busyInputMode).toBe("steer");
    bridge.state.mtime = 200;
    await clock.advance(POLL_MS - 1);
    expect(uiStore.getState().detailsMode).toBe("collapsed");
    await clock.advance(1);
    expect(uiStore.getState().busyInputMode).toBe("interrupt");
    expect(uiStore.getState().detailsMode).toBe("expanded");
    sync.stop();
    expect(clock.pending).toBe(0);
  });

  it("a failing poll keeps the old settings, then heals", async () => {
    const clock = new ManualClock();
    const bridge = fakeBridge({ busy: "steer" });
    const sync = new ConfigSync({ rpc: bridge.rpc, clock });
    await sync.start();
    bridge.state.failMtime = true;
    bridge.state.mtime = 300;
    bridge.state.full = { busy: "interrupt" };
    await clock.advance(POLL_MS);
    expect(uiStore.getState().busyInputMode).toBe("steer");
    bridge.state.failMtime = false;
    bridge.state.failFull = true;
    await clock.advance(POLL_MS);
    expect(uiStore.getState().busyInputMode).toBe("steer");
    bridge.state.failFull = false;
    await clock.advance(POLL_MS);
    expect(uiStore.getState().busyInputMode).toBe("interrupt");
    sync.stop();
  });

  it("a missing busy key applies queue", async () => {
    const bridge = fakeBridge({ indicator: "dots" });
    const sync = new ConfigSync({ rpc: bridge.rpc, clock: new ManualClock() });
    await sync.start();
    expect(uiStore.getState().busyInputMode).toBe("queue");
    expect(uiStore.getState().indicatorStyle).toBe("dots");
    sync.stop();
  });

  it("an MCP revision change reloads once; a failed reload is retried", async () => {
    const clock = new ManualClock();
    const bridge = fakeBridge({ mcp_revision: "a" });
    const reloaded = vi.fn();
    const sync = new ConfigSync({ rpc: bridge.rpc, clock, onMcpReloaded: reloaded });
    await sync.start();
    const reloads = () => bridge.calls.filter(([m]) => m === "reload.mcp").length;
    expect(reloads()).toBe(0);
    bridge.state.full = { mcp_revision: "b" };
    bridge.state.mtime = 101;
    bridge.state.failReload = true;
    await clock.advance(POLL_MS);
    expect(reloads()).toBe(1);
    expect(reloaded).not.toHaveBeenCalled();
    bridge.state.failReload = false;
    await clock.advance(POLL_MS);
    expect(reloads()).toBe(2);
    expect(reloaded).toHaveBeenCalledWith("MCP servers reconnected.");
    await clock.advance(POLL_MS);
    expect(reloads()).toBe(2);
    sync.stop();
  });

  it("drops a full read that lands after the session changed", async () => {
    let release: (value: unknown) => void = () => {};
    const call = vi.fn(async (_method: string, params: unknown) => {
      if ((params as { key: string }).key === "mtime") return { key: "mtime", value: 1 };
      return new Promise((resolve) => (release = resolve));
    });
    const sync = new ConfigSync({ rpc: { call }, clock: new ManualClock() });
    const started = sync.start();
    await settle();
    uiStore.getState().patch({ sid: "s-2", revision: 1 });
    release({ key: "full", value: { busy: "steer" } });
    await started;
    expect(uiStore.getState().busyInputMode).toBe("interrupt");
    sync.stop();
  });
});
