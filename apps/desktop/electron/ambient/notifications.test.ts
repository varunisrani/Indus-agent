import { describe, expect, it } from "vitest";
import {
  type CommandRunner,
  GdbusClient,
  gvariantString,
  gvariantStringArray,
  type NotificationSignal,
  parseBooleanReply,
  parseNotificationSignal,
  parseStringArrayReply,
  parseUint32Reply,
  type RunResult,
} from "./dbus.ts";
import {
  type ElectronNotificationLike,
  type ElectronNotificationOptions,
  NotificationCenter,
} from "./notifications.ts";

class FakeNotification implements ElectronNotificationLike {
  shown = false;
  closed = false;
  readonly handlers = new Map<string, (...args: never[]) => void>();
  readonly options: ElectronNotificationOptions;
  constructor(options: ElectronNotificationOptions) {
    this.options = options;
  }
  show(): void {
    this.shown = true;
  }
  close(): void {
    this.closed = true;
    this.handlers.get("close")?.();
  }
  on(event: string, listener: (...args: never[]) => void): unknown {
    this.handlers.set(event, listener);
    return this;
  }
}

function center(platform: string, dbus: unknown = null) {
  const made: FakeNotification[] = [];
  const activated: Array<[number, string | null, string | null]> = [];
  const state = { enabled: true };
  const notifications = new NotificationCenter({
    platform,
    appName: "Indus",
    isSupported: () => true,
    create: (options) => {
      const n = new FakeNotification(options);
      made.push(n);
      return n;
    },
    dbus: dbus as never,
    enabled: () => state.enabled,
    activate: (senderId, tag, actionId) => activated.push([senderId, tag, actionId]),
    log: () => {},
  });
  return { notifications, made, activated, state };
}

describe("notifications", () => {
  it("disabled → nothing shown", async () => {
    const { notifications, made, state } = center("win32");
    state.enabled = false;
    expect(await notifications.show(1, { title: "x" })).toEqual({ shown: false });
    expect(made).toHaveLength(0);
  });

  it("silent; the same tag replaces the previous one; a click activates the asking window", async () => {
    const { notifications, made, activated } = center("win32");
    await notifications.show(7, { title: "a", tag: "turn:s1" });
    await notifications.show(7, { title: "b", tag: "turn:s1" });
    expect(made[0]?.closed).toBe(true);
    expect(made[1]?.options).toEqual({ title: "b", body: "", silent: true });
    (made[1] as FakeNotification).handlers.get("click")?.();
    expect(activated).toEqual([[7, "turn:s1", null]]);
  });

  it("macOS shows the actions natively and maps the pressed one", async () => {
    const { notifications, made, activated } = center("darwin");
    await notifications.show(3, {
      title: "Ask",
      tag: "ask:s2",
      actions: [
        { id: "open", label: "Open" },
        { id: "later", label: "Later" },
      ],
    });
    expect(made[0]?.options.actions).toEqual([
      { type: "button", text: "Open" },
      { type: "button", text: "Later" },
    ]);
    const action = (made[0] as FakeNotification).handlers.get("action") as unknown as (
      e: unknown,
      i: number,
    ) => void;
    action({}, 1);
    action({}, 9);
    expect(activated).toEqual([[3, "ask:s2", "later"]]);
  });

  it("Windows shows no action buttons (click only)", async () => {
    const { notifications, made } = center("win32");
    await notifications.show(3, { title: "Ask", actions: [{ id: "open", label: "Open" }] });
    expect(made[0]?.options).not.toHaveProperty("actions");
  });
});

class FakeRunner implements CommandRunner {
  readonly calls: string[][] = [];
  answers: Array<(args: readonly string[]) => RunResult | null> = [];
  lineHandler: ((line: string) => void) | null = null;
  ended: (() => void) | null = null;
  stopped = 0;
  run(file: string, args: readonly string[]): Promise<RunResult> {
    this.calls.push([file, ...args]);
    for (const answer of this.answers) {
      const result = answer(args);
      if (result !== null) return Promise.resolve(result);
    }
    return Promise.resolve({ code: 1, stdout: "", stderr: "" });
  }
  lines(
    _file: string,
    _args: readonly string[],
    onLine: (line: string) => void,
    onExit: () => void,
  ) {
    this.lineHandler = onLine;
    this.ended = onExit;
    return {
      stop: () => {
        this.stopped += 1;
      },
    };
  }
}

const method = (args: readonly string[]) => args[args.indexOf("--method") + 1] ?? "";

describe("Linux notifications over D-Bus", () => {
  function linux(caps: string) {
    const runner = new FakeRunner();
    let nextId = 40;
    runner.answers.push((args) =>
      method(args).endsWith("GetCapabilities") ? { code: 0, stdout: caps, stderr: "" } : null,
    );
    runner.answers.push((args) =>
      method(args).endsWith(".Notify")
        ? { code: 0, stdout: `(uint32 ${++nextId},)\n`, stderr: "" }
        : null,
    );
    const client = new GdbusClient(runner);
    return { runner, ...center("linux", client as never) };
  }

  it("sends actions when the service has them and follows the answers", async () => {
    const { runner, notifications, activated, made } = linux("(['actions', 'body'],)\n");
    expect(
      await notifications.show(5, {
        title: "Ask 'quoted'",
        body: "line\none",
        tag: "ask:s1",
        actions: [{ id: "deny", label: "Deny" }],
      }),
    ).toEqual({ shown: true });
    expect(made).toHaveLength(0);
    const notify = runner.calls.find((c) => method(c).endsWith(".Notify")) as string[];
    expect(notify).toContain("'Ask \\'quoted\\''");
    expect(notify).toContain("'line\\u000aone'");
    expect(notify).toContain("['default', 'Open', 'deny', 'Deny']");
    expect(notify.includes("--session")).toBe(true);

    runner.lineHandler?.(
      "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 41, 'deny')",
    );
    runner.lineHandler?.(
      "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 41, 'evil')",
    );
    runner.lineHandler?.(
      "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 99, 'default')",
    );
    runner.lineHandler?.(
      "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 41, 'default')",
    );
    expect(activated).toEqual([
      [5, "ask:s1", "deny"],
      [5, "ask:s1", null],
    ]);

    // Same tag again: the service replaces it in place.
    await notifications.show(5, { title: "Again", tag: "ask:s1" });
    const second = runner.calls.filter((c) => method(c).endsWith(".Notify"))[1] as string[];
    expect(second).toContain("41");
    notifications.dispose();
    expect(runner.stopped).toBe(1);
  });

  it("no D-Bus service → a plain Electron notification", async () => {
    const runner = new FakeRunner();
    const { notifications, made } = center("linux", new GdbusClient(runner) as never);
    expect(await notifications.show(1, { title: "x" })).toEqual({ shown: true });
    expect(made).toHaveLength(1);
  });

  it("without the actions capability no monitor runs and no actions are sent", async () => {
    const { runner, notifications } = linux("(['body'],)");
    await notifications.show(1, { title: "x", actions: [{ id: "a", label: "A" }] });
    expect(runner.lineHandler).toBeNull();
    const notify = runner.calls.find((c) => method(c).endsWith(".Notify")) as string[];
    expect(notify).toContain("@as []");
  });
});

describe("D-Bus text", () => {
  it("quotes GVariant strings", () => {
    expect(gvariantString("a'b\\c")).toBe("'a\\'b\\\\c'");
    expect(gvariantString("tab\there")).toBe("'tab\\u0009here'");
    expect(gvariantString("ünï")).toBe("'ünï'");
    expect(gvariantStringArray([])).toBe("@as []");
  });

  it("parses replies", () => {
    expect(parseBooleanReply("(<true>,)\n")).toBe(true);
    expect(parseBooleanReply("(false,)")).toBe(false);
    expect(parseBooleanReply("garbage")).toBeNull();
    expect(parseUint32Reply("(uint32 7,)")).toBe(7);
    expect(parseUint32Reply("(7,)")).toBeNull();
    expect(parseStringArrayReply("(['actions', 'it\\'s'],)")).toEqual(["actions", "it's"]);
    expect(parseStringArrayReply("(@as [],)")).toEqual([]);
    const closed: NotificationSignal | null = parseNotificationSignal(
      "/org/freedesktop/Notifications: org.freedesktop.Notifications.NotificationClosed (uint32 3, uint32 2)",
    );
    expect(closed).toEqual({ kind: "closed", id: 3 });
    expect(parseNotificationSignal("something else")).toBeNull();
  });

  it("asks for the StatusNotifier host with properties Get", async () => {
    const runner = new FakeRunner();
    runner.answers.push((args) =>
      method(args) === "org.freedesktop.DBus.Properties.Get"
        ? { code: 0, stdout: "(<true>,)", stderr: "" }
        : null,
    );
    expect(await new GdbusClient(runner).statusNotifierHost()).toEqual({ ok: true });
    expect(runner.calls[0]).toEqual([
      "gdbus",
      "call",
      "--session",
      "--dest",
      "org.kde.StatusNotifierWatcher",
      "--object-path",
      "/StatusNotifierWatcher",
      "--method",
      "org.freedesktop.DBus.Properties.Get",
      "org.kde.StatusNotifierWatcher",
      "IsStatusNotifierHostRegistered",
    ]);
    const none = new FakeRunner();
    none.answers.push(() => ({ code: 0, stdout: "(<false>,)", stderr: "" }));
    expect(await new GdbusClient(none).statusNotifierHost()).toEqual({
      ok: false,
      reason: "no system tray host is registered",
    });
    const missing = new FakeRunner();
    missing.answers.push(() => ({ code: null, stdout: "", stderr: "" }));
    expect((await new GdbusClient(missing).statusNotifierHost()).ok).toBe(false);
  });
});
