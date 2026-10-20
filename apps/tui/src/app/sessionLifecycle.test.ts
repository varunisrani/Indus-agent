import { afterEach, describe, expect, it } from "vitest";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import {
  BUSY_SWITCH_LINE,
  historyRows,
  STATUS_RECOVERING,
  visibleStored,
} from "./sessionLifecycle.ts";
import { notFound, type RuntimeRig, resumeAnswer, runtimeRig, settle } from "./testRuntime.ts";

let rig: RuntimeRig | null = null;
const use = (made: RuntimeRig): RuntimeRig => {
  rig = made;
  return made;
};
afterEach(() => {
  rig?.runtime.dispose();
  rig = null;
});

const lines = (): string[] =>
  transcriptStore.getState().messages.map((m) => `${m.role}: ${m.text}`);

describe("bootstrap order", () => {
  it("creates a session with the declared launch options and a fresh key", async () => {
    const r = use(
      runtimeRig({
        env: {
          INDUS_MODEL: "m-1",
          INDUS_TUI_TOOLSETS: "web,files",
          INDUS_TUI_SKILLS: "a,b",
          INDUS_TUI_CWD: "/w",
        },
      }),
    );
    await r.ready();
    expect(r.calls("client.capabilities")).toEqual([
      { features: ["server_requests"], surface: "tui" },
    ]);
    expect(r.calls("session.create")).toEqual([
      { model: "m-1", toolsets: ["web", "files"], cwd: "/w", idempotency_key: "key-1" },
    ]);
    expect(uiStore.getState().sid).toMatch(/^s-new-/);
    expect(lines().some((l) => l.includes("left out: skills"))).toBe(true);
    // capabilities are declared before any session call
    const order = r.server.received.map((f) => f.method);
    expect(order.indexOf("client.capabilities")).toBeLessThan(order.indexOf("session.create"));
  });

  it("resumes the launcher's id, then sends the startup prompt literally", async () => {
    const r = use(runtimeRig({ env: { INDUS_TUI_RESUME: "s-old", INDUS_TUI_QUERY: "/help" } }));
    await r.ready();
    expect(r.calls("session.resume")).toEqual([{ session_id: "s-old" }]);
    expect(r.calls("session.create")).toEqual([]);
    expect(r.calls("prompt.submit")).toEqual([{ session_id: "s-old", text: "/help" }]);
    expect(r.calls("slash.exec")).toEqual([]);
    expect(lines()).toContain("user: /help");
  });

  it("a refused launcher resume id says so and starts a new session", async () => {
    const r = use(
      runtimeRig({
        env: { INDUS_TUI_RESUME: "s-gone" },
        handlers: {
          "session.resume": (p) => {
            throw notFound(String(p.session_id));
          },
        },
      }),
    );
    await r.ready();
    expect(r.calls("session.create")).toHaveLength(1);
    expect(lines()).toContain("system: error: could not resume s-gone: no session s-gone");
  });

  it("never sends the startup prompt twice, nor after a recovery", async () => {
    const r = use(runtimeRig({ env: { INDUS_TUI_QUERY: "hello {!date}" } }));
    await r.ready();
    expect(r.calls("prompt.submit")).toEqual([
      { session_id: uiStore.getState().sid, text: "hello {!date}" },
    ]);
    expect(r.calls("shell.exec")).toEqual([]);
    await r.die();
    await r.ready();
    expect(r.calls("session.resume")).toHaveLength(1);
    expect(r.calls("prompt.submit")).toHaveLength(1);
  });

  it("attaches the startup image before the prompt; a failed attach stops it", async () => {
    const ok = use(
      runtimeRig({ env: { INDUS_TUI_IMAGE: "/tmp/p.png", INDUS_TUI_QUERY: "what?" } }),
    );
    ok.server.handle("image.attach", () => ({ attached: 1, name: "p.png", path: "/tmp/p.png" }));
    await ok.ready();
    const order = ok.server.received.map((f) => f.method);
    expect(order.indexOf("image.attach")).toBeLessThan(order.indexOf("prompt.submit"));
    ok.runtime.dispose();

    const bad = use(runtimeRig({ env: { INDUS_TUI_IMAGE: "/missing.png" } }));
    await bad.ready();
    expect(bad.calls("prompt.submit")).toEqual([]);
    expect(lines().some((l) => l.includes("the image did not attach"))).toBe(true);
  });

  it("auto-resumes the most recent session only when the setting is on", async () => {
    const on = use(
      runtimeRig({
        handlers: {
          "config.get": (p) => ({ key: p.key, value: p.key === "display.tui_auto_resume_recent" }),
          "session.most_recent": () => ({ session: { session_id: "s-recent", started_at: 1 } }),
        },
      }),
    );
    await on.ready();
    expect(on.calls("session.resume")).toEqual([{ session_id: "s-recent" }]);
    expect(on.calls("session.create")).toEqual([]);
    on.runtime.dispose();

    const off = use(runtimeRig());
    await off.ready();
    expect(off.calls("session.most_recent")).toEqual([]);
    expect(off.calls("session.create")).toHaveLength(1);
  });

  it("a recovery target wins over the launcher's resume id and shows the recovering status", async () => {
    const r = use(
      runtimeRig({
        env: { INDUS_TUI_RESUME: "s-launch" },
        handlers: { "session.resume": (p) => resumeAnswer(String(p.session_id)) },
      }),
    );
    await r.ready();
    expect(uiStore.getState().sid).toBe("s-launch");
    await r.die();
    r.server.handle("session.resume", () => new Promise(() => {}));
    r.server.ready();
    await settle(40);
    expect(uiStore.getState().statusText).toBe(STATUS_RECOVERING);
    expect(r.calls("session.resume").at(-1)).toEqual({ session_id: "s-launch" });
  });
});

describe("tool progress after create", () => {
  it("applies a valid mode to the new session and reports an invalid one", async () => {
    const r = use(runtimeRig({ env: { INDUS_TUI_TOOL_PROGRESS: "off" } }));
    r.server.handle("config.set", (p) => ({ key: p.key, value: p.value }));
    await r.ready();
    expect(r.calls("config.set")).toEqual([
      { key: "verbose", value: "off", session_id: uiStore.getState().sid },
    ]);
    r.runtime.dispose();
    const bad = use(runtimeRig({ env: { INDUS_TUI_TOOL_PROGRESS: "loud" } }));
    await bad.ready();
    expect(bad.calls("config.set")).toEqual([]);
    expect(lines().some((l) => l.includes("tool progress mode loud"))).toBe(true);
  });
});

describe("create retries (R05.21)", () => {
  it("a lost create answer is retried with the same idempotency key", async () => {
    let first = true;
    const r = use(
      runtimeRig({
        timeoutMs: 50,
        handlers: {
          "session.create": () => {
            if (first) {
              first = false;
              return new Promise(() => {});
            }
            return { session_id: "s-once", created: false };
          },
        },
      }),
    );
    r.server.ready();
    await new Promise((resolve) => setTimeout(resolve, 150));
    await settle();
    const keys = r.calls("session.create").map((p) => p.idempotency_key);
    expect(keys).toEqual(["key-1", "key-1"]);
    expect(uiStore.getState().sid).toBe("s-once");
  });

  it("a create cut by a child death reuses its key after the restart", async () => {
    let calls = 0;
    const r = use(
      runtimeRig({
        handlers: {
          "session.create": () => {
            calls += 1;
            return calls === 1 ? new Promise(() => {}) : { session_id: "s-2", created: true };
          },
        },
      }),
    );
    await r.ready();
    await r.die();
    await r.ready();
    expect(r.calls("session.create").map((p) => p.idempotency_key)).toEqual(["key-1", "key-1"]);
    expect(uiStore.getState().sid).toBe("s-2");
  });

  it("a refused create starts over with a new key next time", async () => {
    const r = use(
      runtimeRig({
        handlers: {
          "session.create": () => {
            throw notFound("x");
          },
        },
      }),
    );
    await r.ready();
    await r.runtime.lifecycle.create();
    expect(r.calls("session.create").map((p) => p.idempotency_key)).toEqual(["key-1", "key-2"]);
    expect(lines().some((l) => l.startsWith("system: error: could not start a session"))).toBe(
      true,
    );
  });
});

describe("session changes", () => {
  it("each change writes the stored id to the active-session file", async () => {
    const r = use(runtimeRig({ env: { INDUS_TUI_ACTIVE_SESSION_FILE: "/tmp/active.json" } }));
    await r.ready();
    const first = uiStore.getState().sid;
    await r.runtime.lifecycle.switchTo("s-other");
    expect(r.files.map((f) => JSON.parse(f.text))).toEqual([
      { session_id: first },
      { session_id: "s-other" },
    ]);
    expect(r.files.every((f) => f.path === "/tmp/active.json")).toBe(true);
  });

  it("a resume fills the transcript from the stored rows", async () => {
    const r = use(
      runtimeRig({
        env: { INDUS_TUI_RESUME: "s-h" },
        handlers: {
          "session.resume": (p) =>
            resumeAnswer(String(p.session_id), [
              { role: "user", text: "hi" },
              { role: "assistant", text: "hello" },
            ]),
        },
      }),
    );
    transcriptStore.getState().append({ role: "system", text: "stale" });
    await r.ready();
    expect(lines()).toEqual(["user: hi", "assistant: hello"]);
  });

  it("switching is refused while a turn runs", async () => {
    const r = use(runtimeRig());
    await r.ready();
    uiStore.getState().patch({ busy: true });
    expect(await r.runtime.lifecycle.switchTo("s-x")).toBeNull();
    expect(await r.runtime.lifecycle.newSession()).toBeNull();
    expect(r.calls("session.resume")).toEqual([]);
    expect(lines().at(-1)).toBe(`system: ${BUSY_SWITCH_LINE}`);
  });

  it("/new closes the session it leaves; the switcher's new keeps it live", async () => {
    const r = use(runtimeRig());
    await r.ready();
    const first = uiStore.getState().sid;
    await r.runtime.lifecycle.newSession(undefined, true);
    expect(r.calls("session.close")).toEqual([]);
    const second = uiStore.getState().sid;
    expect(r.runtime.lifecycle.liveSessions().map((s) => s.id)).toEqual([second, first]);
    await r.runtime.lifecycle.newSession("titled");
    await settle();
    expect(r.calls("session.close")).toEqual([{ session_id: second }]);
    expect(r.calls("session.create").at(-1)).toMatchObject({ title: "titled" });
  });

  it("maps stored rows and hides delegated sources", () => {
    expect(
      historyRows([
        { role: "user", text: "q" },
        { role: "tool", text: "out", name: "read_file" },
      ]),
    ).toEqual([
      { role: "user", text: "q" },
      { role: "tool", text: "read_file: out" },
    ]);
    expect(
      visibleStored([
        { session_id: "a", started_at: 1, source: "tui" },
        { session_id: "b", started_at: 1, source: "subagent" },
      ]).map((row) => row.session_id),
    ).toEqual(["a"]);
  });

  it("deleting a stored session reports a bridge without the method", async () => {
    const r = use(runtimeRig());
    await r.ready();
    expect(await r.runtime.lifecycle.deleteStored("s-old")).toContain("not available");
  });
});
