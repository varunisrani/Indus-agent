import { describe, expect, it, vi } from "vitest";
import type { ModelPickerPayload } from "../cards/payloads.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { createDispatcher } from "./dispatch.ts";
import {
  LOCAL_COMMANDS,
  NO_CONFIRM_ENV,
  UPDATE_DELAY_MS,
  UPDATE_EXIT_CODE,
} from "./localCommands.ts";
import { entry, slashSetup } from "./testContext.ts";

const SPEC_NAMES = [
  "help",
  "quit",
  "exit",
  "update",
  "clear",
  "new",
  "redraw",
  "status",
  "title",
  "details",
  "copy",
  "paste",
  "prompt",
  "logs",
  "history",
  "save",
  "statusbar",
  "queue",
  "steer",
  "undo",
  "retry",
  "bg",
  "btw",
  "model",
  "sessions",
  "image",
  "compress",
  "branch",
  "theme",
  "skin",
  "yolo",
  "reasoning",
  "fast",
  "busy",
  "verbose",
  "usage",
  "stop",
  "reload-mcp",
  "reload",
  "tools",
  "skills",
  "mouse",
];

describe("the local registry", () => {
  it("holds every command this phase names, once", () => {
    const names = LOCAL_COMMANDS.flatMap((c) => [c.name, ...(c.aliases ?? [])]);
    for (const name of SPEC_NAMES) expect(names).toContain(name);
    expect(new Set(names).size).toBe(names.length);
  });
});

describe("leaving", () => {
  it("/update says so and exits 42 after 100 ms", async () => {
    const { ctx, out } = slashSetup();
    await createDispatcher(ctx).run("/update");
    expect(out.lines).toHaveLength(1);
    expect(out.exits).toEqual([]);
    expect(out.timers.map((t) => t.ms)).toEqual([UPDATE_DELAY_MS]);
    out.timers[0]?.run();
    expect(out.exits).toEqual([UPDATE_EXIT_CODE]);
    expect(UPDATE_EXIT_CODE).toBe(42);
  });

  it("/quit exits 0", async () => {
    const { ctx, out } = slashSetup();
    await createDispatcher(ctx).run("/quit");
    expect(out.exits).toEqual([0]);
  });
});

describe("destructive commands", () => {
  it("/clear asks first and starts a session only on yes", async () => {
    const { ctx, confirm } = slashSetup();
    const newSession = vi.fn();
    confirm.mockResolvedValueOnce(false);
    const dispatcher = createDispatcher({ ...ctx, newSession });
    await dispatcher.run("/clear");
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ danger: true }));
    expect(newSession).not.toHaveBeenCalled();
    await dispatcher.run("/clear");
    expect(newSession).toHaveBeenCalledWith(undefined);
  });

  it("/new passes its title", async () => {
    const { ctx } = slashSetup();
    const newSession = vi.fn();
    await createDispatcher({ ...ctx, newSession }).run("/new Sprint notes");
    expect(newSession).toHaveBeenCalledWith("Sprint notes");
  });

  it(`${NO_CONFIRM_ENV} skips the question`, async () => {
    const { ctx, confirm } = slashSetup();
    const newSession = vi.fn();
    await createDispatcher({ ...ctx, newSession, env: { [NO_CONFIRM_ENV]: "1" } }).run("/new");
    expect(confirm).not.toHaveBeenCalled();
    expect(newSession).toHaveBeenCalledTimes(1);
  });

  it("is refused while a reply runs", async () => {
    const { ctx, out, confirm } = slashSetup();
    uiStore.getState().patch({ busy: true });
    const newSession = vi.fn();
    await createDispatcher({ ...ctx, newSession }).run("/clear");
    expect(out.lines[0]).toContain("stop the turn first");
    expect(confirm).not.toHaveBeenCalled();
    expect(newSession).not.toHaveBeenCalled();
  });
});

describe("/model", () => {
  it("opens the picker without touching the composer, before and after it closes", async () => {
    const { ctx, out } = slashSetup();
    await createDispatcher(ctx).run("/model");
    const card = overlayStore.getState().card;
    expect(card?.kind).toBe("modelPicker");
    expect(card?.payload).toEqual({
      sessionId: "s-1",
      refresh: false,
    } satisfies ModelPickerPayload);
    overlayStore.getState().close(card?.id ?? "");
    expect(overlayStore.getState().card).toBeNull();
    expect(out.prefills).toEqual([]);
    expect(out.submits).toEqual([]);
  });

  it("--refresh asks the picker for a fresh list", async () => {
    const { ctx } = slashSetup();
    await createDispatcher(ctx).run("/model --refresh");
    expect(overlayStore.getState().card?.payload).toMatchObject({ refresh: true });
  });

  it("a spec switches directly", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("config.set", (p) => ({
      key: "model",
      value: p.value,
      applied: "now",
      saved: false,
    }));
    await createDispatcher(ctx).run("/model orbit:orbit-small");
    expect(server.calls("config.set")[0]?.params).toEqual({
      key: "model",
      value: "orbit:orbit-small",
      session_id: "s-1",
    });
    expect(out.lines[0]).toContain("orbit:orbit-small");
    expect(overlayStore.getState().card).toBeNull();
  });
});

describe("settings commands", () => {
  it("/busy changes the store and the settings file", async () => {
    const { server, ctx } = slashSetup();
    server.handle("config.set", (p) => ({
      key: p.key,
      value: p.value,
      applied: "now",
      saved: true,
    }));
    await createDispatcher(ctx).run("/busy steer");
    expect(uiStore.getState().busyInputMode).toBe("steer");
    expect(server.calls("config.set")[0]?.params).toEqual({ key: "busy", value: "steer" });
  });

  it("/details cycles and keeps hidden out of the settings file", async () => {
    const { server, ctx } = slashSetup();
    server.handle("config.set", (p) => ({
      key: p.key,
      value: p.value,
      applied: "now",
      saved: true,
    }));
    uiStore.getState().patch({ detailsMode: "expanded" });
    await createDispatcher(ctx).run("/details");
    expect(uiStore.getState().detailsMode).toBe("hidden");
    expect(server.calls("config.set")).toHaveLength(0);
    await createDispatcher(ctx).run("/details expanded");
    expect(server.calls("config.set")[0]?.params).toEqual({ key: "details", value: "expanded" });
  });

  it("/statusbar bottom moves the bar; the file keeps on/off", async () => {
    const { server, ctx } = slashSetup();
    server.handle("config.set", (p) => ({
      key: p.key,
      value: p.value,
      applied: "now",
      saved: true,
    }));
    await createDispatcher(ctx).run("/statusbar bottom");
    expect(uiStore.getState().statusBar).toBe("bottom");
    expect(server.calls("config.set")[0]?.params).toEqual({ key: "statusbar", value: true });
  });

  it("/reasoning show is local; a level goes to the session", async () => {
    const { server, ctx } = slashSetup();
    server.handle("config.set", (p) => ({
      key: p.key,
      value: p.value,
      applied: "now",
      saved: false,
    }));
    await createDispatcher(ctx).run("/reasoning show");
    expect(uiStore.getState().showReasoning).toBe(true);
    await createDispatcher(ctx).run("/reasoning high");
    expect(server.calls("config.set")[0]?.params).toEqual({
      key: "reasoning",
      value: "high",
      session_id: "s-1",
    });
  });

  it("/yolo with no argument flips the current value", async () => {
    const { server, ctx } = slashSetup();
    server.handle("config.get", () => ({ key: "yolo", value: false }));
    server.handle("config.set", (p) => ({
      key: p.key,
      value: p.value,
      applied: "now",
      saved: false,
    }));
    await createDispatcher(ctx).run("/yolo");
    expect(server.calls("config.set")[0]?.params).toEqual({
      key: "yolo",
      value: true,
      session_id: "s-1",
    });
  });
});

describe("session commands", () => {
  it("/undo puts the text back into the composer", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("session.undo", () => ({ removed: 2, prefill: "my last question" }));
    await createDispatcher(ctx).run("/undo");
    expect(out.prefills).toEqual(["my last question"]);
  });

  it("/retry sends the last message again", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("session.undo", () => ({ removed: 2, prefill: "my last question" }));
    await createDispatcher(ctx).run("/retry");
    expect(out.submits).toEqual([{ body: "my last question", shown: undefined }]);
  });

  it("/steer on an idle session sends the text as a message", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("session.steer", () => ({ queued: false }));
    await createDispatcher(ctx).run("/steer use tabs");
    expect(out.submits).toEqual([{ body: "use tabs", shown: undefined }]);
  });

  it("/branch switches to the new session", async () => {
    const { server, ctx } = slashSetup();
    server.handle("session.branch", () => ({ session_id: "s-9", title: "copy" }));
    const switchSession = vi.fn();
    await createDispatcher({ ...ctx, switchSession }).run("/branch copy");
    expect(server.calls("session.branch")[0]?.params).toEqual({ session_id: "s-1", title: "copy" });
    expect(switchSession).toHaveBeenCalledWith("s-9");
  });

  it("/stop, /tools and /skills run on the bridge", async () => {
    const { server, ctx } = slashSetup();
    server.handle("slash.exec", () => ({ output: "ok" }));
    const dispatcher = createDispatcher(ctx);
    await dispatcher.run("/stop");
    await dispatcher.run("/tools list");
    expect(server.calls("slash.exec").map((f) => f.params)).toEqual([
      { session_id: "s-1", command: "stop" },
      { session_id: "s-1", command: "tools list" },
    ]);
  });

  it("/save writes the history to a file", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("session.history", () => ({ messages: [{ role: "user", text: "hi" }] }));
    const writeFile = vi.fn(async () => {});
    await createDispatcher({ ...ctx, writeFile }).run("/save chat.json");
    expect(writeFile).toHaveBeenCalledWith("chat.json", expect.stringContaining('"s-1"'));
    expect(out.lines[0]).toContain("chat.json");
  });
});

describe("help and missing hooks", () => {
  it("/help pages local and bridge commands", async () => {
    const { ctx, out } = slashSetup({
      catalog: [entry("deploy", { description: "ship it" }), entry("help")],
    });
    await createDispatcher(ctx).run("/help");
    expect(out.pages).toHaveLength(1);
    expect(out.pages[0]?.text).toContain("/quit, /exit");
    expect(out.pages[0]?.text).toContain("/deploy — ship it");
  });

  it("/help leaves out what the server marks unavailable here (W48-h)", async () => {
    const { ctx, out } = slashSetup({
      catalog: [entry("deploy"), entry("gs", { runs: "unavailable", description: "git status" })],
    });
    await createDispatcher(ctx).run("/help");
    expect(out.pages[0]?.text).toContain("/deploy");
    expect(out.pages[0]?.text).not.toContain("/gs");
  });

  it("a command whose hook is missing says it is not available yet", async () => {
    const { ctx, out } = slashSetup();
    await createDispatcher(ctx).run("/copy");
    expect(out.lines[0]).toContain("not available");
  });
});
