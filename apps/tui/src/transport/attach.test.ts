// P17-19: the TUI's attach bootstrap — transport choice, grant verdicts, describe + one ready, the
// recover budget, the startup fallback to the local child, and the app's reaction to a terminal
// verdict.

import {
  type ConnectHooks,
  GATEWAY_SUBPROTOCOL,
  IndusClient,
  LogRing,
  TICKET_SUBPROTOCOL_PREFIX,
  type TransportEvent,
  type WebSocketLike,
  WsTransport,
} from "@indus/client";
import { FakeServer } from "@indus/client/testing";
import { afterEach, describe, expect, it } from "vitest";
import { AppRuntime, DAEMON_UNAVAILABLE_STATUS, LinkWatch } from "../app/appRuntime.ts";
import { RESTART_LINE } from "../app/recovery.ts";
import { FakeClock, settle } from "../composer/testSupport.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { resetStores } from "../testing/harness.ts";
import {
  ATTACH_ENV,
  AttachTransport,
  attachConnector,
  attachFailureLines,
  chooseTransport,
  GRANT_MAX_BYTES,
  GRANT_TIMEOUT_MS,
  type GrantMode,
  type GrantOutcome,
  grantRunner,
  helperEnv,
  parseGrant,
  RecoverBudget,
} from "./attach.ts";

const URL_OK = "ws://127.0.0.1:47011/ws";
const ticket = (): string => `k${"Q".repeat(24)}${Math.random().toString(36).slice(2, 12)}`;

function grantJson(secret: string, extra: Record<string, unknown> = {}): string {
  return JSON.stringify({
    url: URL_OK,
    protocols: [GATEWAY_SUBPROTOCOL, TICKET_SUBPROTOCOL_PREFIX + secret],
    profile_id: "prof-1",
    instance_id: "inst-1",
    ...extra,
  });
}

const okGrant = (secret = ticket(), instance = "inst-1"): GrantOutcome => ({
  ok: true,
  grant: {
    url: URL_OK,
    protocols: [GATEWAY_SUBPROTOCOL, TICKET_SUBPROTOCOL_PREFIX + secret],
    profile_id: "prof-1",
    instance_id: instance,
  },
});

const terminal: GrantOutcome = {
  ok: false,
  terminal: true,
  code: "ws_unavailable",
  message: "the daemon has no attach endpoint",
  hint: "Restart it with `indus gateway restart`, or run `indus --tui --local`.",
};

const retry: GrantOutcome = {
  ok: false,
  terminal: false,
  code: "timeout",
  message: "no answer",
  hint: null,
};

function hooks(): ConnectHooks & { events: TransportEvent[] } {
  const events: TransportEvent[] = [];
  return { logs: new LogRing(), onEvent: (event) => events.push(event), events };
}

/** The daemon side: a FakeServer whose ready names the profile and instance. */
function daemon(describe: "ok" | "missing" | "error" = "ok", instance = "inst-1") {
  const server = new FakeServer();
  if (describe === "ok") {
    server.handle("runtime.describe", () => ({ creatable_sources: ["tui"], profile_id: "prof-1" }));
  } else if (describe === "error") {
    server.handle("runtime.describe", () => {
      throw new Error("boom");
    });
  }
  const sayReady = () =>
    server.sendRaw({
      jsonrpc: "2.0",
      method: "gateway.ready",
      params: {
        type: "gateway.ready",
        theme: "auto",
        heartbeat: false,
        protocol_version: 1,
        replay_epoch: "ep-1",
        profile_id: "prof-1",
        instance_id: instance,
        capabilities: ["request.decline"],
      },
    });
  return { server, sayReady };
}

describe("chooseTransport", () => {
  it("explicit URL first, then --local, else the grant helper", () => {
    expect(chooseTransport({ [ATTACH_ENV.url]: URL_OK, [ATTACH_ENV.local]: "1" })).toEqual({
      mode: "explicit",
      url: URL_OK,
    });
    expect(chooseTransport({ [ATTACH_ENV.local]: "1" })).toEqual({ mode: "local" });
    expect(chooseTransport({})).toEqual({
      mode: "daemon",
      command: "indus",
      args: ["gateway", "grant", "--json"],
    });
    expect(chooseTransport({ [ATTACH_ENV.grant]: "/opt/indus/bin/indus" })).toEqual({
      mode: "daemon",
      command: "/opt/indus/bin/indus",
      args: ["gateway", "grant", "--json"],
    });
    expect(chooseTransport({ [ATTACH_ENV.grant]: "python -m indus gateway grant --json" })).toEqual(
      {
        mode: "daemon",
        command: "python",
        args: ["-m", "indus", "gateway", "grant", "--json"],
      },
    );
  });
});

describe("the grant", () => {
  it("parses a grant and keeps the ticket inside protocols", () => {
    const secret = ticket();
    const parsed = parseGrant(grantJson(secret), 0);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.grant.url).toBe(URL_OK);
    expect(parsed.grant.url).not.toContain(secret);
    expect(parsed.grant.protocols[1]).toBe(TICKET_SUBPROTOCOL_PREFIX + secret);
  });

  it("refuses a grant that is not a loopback URL or not our subprotocols", () => {
    const secret = ticket();
    for (const extra of [
      { url: "ws://10.0.0.5:1/ws" },
      { url: `ws://127.0.0.1:1/ws?ticket=${secret}` },
      { url: "ws://user:pw@127.0.0.1:1/ws" },
      { url: "http://127.0.0.1:1/ws" },
      { protocols: [GATEWAY_SUBPROTOCOL] },
      { protocols: ["x", TICKET_SUBPROTOCOL_PREFIX + secret] },
      { profile_id: 3 },
    ]) {
      const parsed = parseGrant(grantJson(secret, extra), 0);
      expect(parsed).toMatchObject({ ok: false, terminal: false, code: "bad_grant" });
      if (!parsed.ok) expect(parsed.message).not.toContain(secret);
    }
  });

  it("reads the helper's verdicts; no JSON at all is terminal and never echoes stdout", () => {
    const failed = (verdict: string) =>
      JSON.stringify({ error: { verdict, code: "c", message: "m", hint: "h" } });
    expect(parseGrant(failed("gateway_unavailable"), 2)).toEqual({
      ok: false,
      terminal: true,
      code: "c",
      message: "m",
      hint: "h",
    });
    expect(parseGrant(failed("retry"), 1)).toMatchObject({ ok: false, terminal: false });
    const junk = parseGrant("Traceback secret-looking-stdout", 1, "ModuleNotFoundError: indus");
    expect(junk).toMatchObject({ ok: false, terminal: true, code: "helper_failed" });
    if (!junk.ok) {
      expect(junk.message).toContain("ModuleNotFoundError");
      expect(junk.message).not.toContain("secret-looking-stdout");
    }
  });

  it("runs the helper with its limits, the mode and a scrubbed env", async () => {
    const seen: { file: string; args: readonly string[]; options: Record<string, unknown> }[] = [];
    const runner = grantRunner({
      command: "indus",
      args: ["gateway", "grant", "--json"],
      env: helperEnv({
        PATH: "/bin",
        INDUS_HOME: "/h",
        INDUS_TUI_QUERY: "hello",
        INDUS_YOLO_MODE: "1",
        INDUS_MODEL: "m",
      }),
      exec: (file, args, options, done) => {
        seen.push({ file, args, options: options as unknown as Record<string, unknown> });
        done(null, grantJson(ticket()), "");
      },
    });
    expect((await runner("start")).ok).toBe(true);
    expect(seen[0]?.args).toEqual(["gateway", "grant", "--json", "--mode", "start"]);
    expect(seen[0]?.options).toMatchObject({
      timeout: GRANT_TIMEOUT_MS,
      maxBuffer: GRANT_MAX_BYTES,
      env: { PATH: "/bin", INDUS_HOME: "/h" },
    });
    expect(Object.keys(seen[0]?.options.env as object)).toEqual(["PATH", "INDUS_HOME"]);
    expect(GRANT_TIMEOUT_MS).toBe(40_000);
    expect(GRANT_MAX_BYTES).toBe(1024 * 1024);
  });

  it("maps a missing program to terminal, a timeout or flood to retry", async () => {
    const failing = (error: Error & { code?: unknown; killed?: boolean }, stdout = "") =>
      grantRunner({
        command: "indus",
        args: [],
        env: {},
        exec: (_f, _a, _o, done) => done(error, stdout, ""),
      })("discover");
    expect(await failing(Object.assign(new Error("x"), { code: "ENOENT" }))).toMatchObject({
      terminal: true,
      code: "helper_failed",
    });
    expect(
      await failing(Object.assign(new Error("x"), { killed: true, code: null })),
    ).toMatchObject({ terminal: false, code: "helper_timeout" });
    expect(
      await failing(Object.assign(new Error("x"), { code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER" })),
    ).toMatchObject({ terminal: false, code: "helper_output" });
    const verdict = JSON.stringify({
      error: { verdict: "gateway_unavailable", code: "unsafe_home", message: "m" },
    });
    expect(await failing(Object.assign(new Error("x"), { code: 2 }), verdict)).toMatchObject({
      terminal: true,
      code: "unsafe_home",
    });
  });

  it("allows 3 re-ensures per 60 s", () => {
    const clock = new FakeClock();
    const budget = new RecoverBudget({ clock });
    expect([budget.take(), budget.take(), budget.take(), budget.take()]).toEqual([
      true,
      true,
      true,
      false,
    ]);
    clock.advance(60_000);
    expect(budget.take()).toBe(true);
  });
});

describe("AttachTransport", () => {
  it("describe, then exactly one synthetic ready; send waits for it", async () => {
    const { server, sayReady } = daemon("ok");
    const h = hooks();
    const transport = new AttachTransport({
      hooks: h,
      source: { grant: async () => okGrant() },
      open: () => server.connect(),
    });
    const frames: Record<string, unknown>[] = [];
    transport.onFrame((frame) => frames.push(frame as Record<string, unknown>));
    expect(() => transport.send({ jsonrpc: "2.0", method: "x" })).toThrow(/not attached/);
    await settle();
    sayReady();
    await settle();
    const describe = server.calls("runtime.describe");
    expect(describe).toHaveLength(1);
    expect(typeof describe[0]?.id).toBe("string");
    expect(frames).toHaveLength(1);
    expect(frames[0]).toMatchObject({
      method: "gateway.ready",
      params: {
        type: "gateway.ready",
        profile_id: "prof-1",
        replay_epoch: "ep-1",
        describe: { creatable_sources: ["tui"] },
      },
    });
    transport.send({ jsonrpc: "2.0", id: 1, method: "gateway.ping" });
    await settle();
    expect(frames.at(-1)).toMatchObject({ id: 1, result: {} });
    sayReady(); // a second ready on the same socket is not published again
    await settle();
    expect(frames.filter((f) => f.method === "gateway.ready")).toHaveLength(1);
  });

  it("a daemon without runtime.describe (-32601) still attaches from its ready", async () => {
    const { server, sayReady } = daemon("missing");
    const transport = new AttachTransport({
      hooks: hooks(),
      source: { grant: async () => okGrant() },
      open: () => server.connect(),
    });
    const frames: Record<string, unknown>[] = [];
    transport.onFrame((frame) => frames.push(frame as Record<string, unknown>));
    await settle();
    sayReady();
    await settle();
    expect(frames).toEqual([
      expect.objectContaining({
        method: "gateway.ready",
        params: expect.objectContaining({ instance_id: "inst-1" }),
      }),
    ]);
  });

  it("a failing describe, another profile or a restarted daemon closes the link", async () => {
    const cases: Array<[ReturnType<typeof daemon>, GrantOutcome, boolean, string]> = [
      [daemon("error"), okGrant(), false, "runtime.describe failed"],
      [daemon("ok", "inst-9"), okGrant(), false, "the daemon restarted between grant and dial"],
    ];
    for (const [{ server, sayReady }, grant, isTerminal, reason] of cases) {
      const transport = new AttachTransport({
        hooks: hooks(),
        source: { grant: async () => grant },
        open: () => server.connect(),
      });
      const closes: unknown[] = [];
      transport.onClose((info) => closes.push(info));
      await settle();
      sayReady();
      await settle();
      expect(closes).toEqual([isTerminal ? { reason, terminal: true } : { reason }]);
    }
    const other = daemon("ok");
    const h = hooks();
    const transport = new AttachTransport({
      hooks: h,
      source: {
        grant: async () => {
          const made = okGrant();
          return made.ok ? { ok: true, grant: { ...made.grant, profile_id: "prof-2" } } : made;
        },
      },
      open: () => other.server.connect(),
    });
    const closes: unknown[] = [];
    transport.onClose((info) => closes.push(info));
    await settle();
    other.sayReady();
    await settle();
    expect(closes).toEqual([{ reason: "attach failed: profile_mismatch", terminal: true }]);
    expect(h.events.at(-1)).toMatchObject({ type: "transport.attach_failed", terminal: true });
  });

  it("a socket that opens but never gets ready, or never describes, is given up at 15 s", async () => {
    for (const answers of [false, true]) {
      const { server, sayReady } = daemon("ok");
      if (answers) server.handle("runtime.describe", () => new Promise(() => {}));
      const clock = new FakeClock();
      const transport = new AttachTransport({
        hooks: hooks(),
        source: { grant: async () => okGrant() },
        open: () => server.connect(),
        clock,
      });
      const closes: unknown[] = [];
      transport.onClose((info) => closes.push(info));
      await settle();
      if (answers) sayReady();
      await settle();
      clock.advance(14_999);
      expect(closes).toEqual([]);
      clock.advance(1);
      expect(closes).toEqual([
        {
          reason: answers ? "runtime.describe did not answer" : "the daemon sent no ready in time",
        },
      ]);
    }
  });

  it("the ticket reaches only the WebSocket's subprotocols: no log line, no event", async () => {
    const secret = ticket();
    const offered: string[][] = [];
    const h = hooks();
    const fakeSocket = (url: string, protocols: string[]): WebSocketLike => {
      offered.push([url, ...protocols]);
      return {
        readyState: 0,
        protocol: "",
        send: () => {},
        close: () => {},
        addEventListener: () => {},
      };
    };
    const transport = new AttachTransport({
      hooks: h,
      source: { grant: async () => okGrant(secret) },
      open: (url, protocols, given) =>
        new WsTransport({
          url,
          protocols,
          webSocket: fakeSocket,
          logs: given.logs,
          onEvent: given.onEvent,
        }),
    });
    await settle();
    expect(offered).toEqual([[URL_OK, GATEWAY_SUBPROTOCOL, TICKET_SUBPROTOCOL_PREFIX + secret]]);
    await transport.close();
    expect(h.logs.tail(100).join("\n")).not.toContain(secret);
    expect(JSON.stringify(h.events)).not.toContain(secret);
  });
});

describe("attachConnector", () => {
  it("asks start first, then recover within the budget, then discover", async () => {
    const modes: GrantMode[] = [];
    const clock = new FakeClock();
    const connect = attachConnector({
      plan: { mode: "daemon", command: "indus", args: [] },
      runGrant: async (mode) => {
        modes.push(mode);
        return retry;
      },
      clock,
    });
    for (let i = 0; i < 6; i += 1) {
      connect(hooks());
      await settle(4);
    }
    expect(modes).toEqual(["start", "recover", "recover", "recover", "discover", "discover"]);
    expect(connect.state.grants).toEqual(modes);
  });

  it("an explicit URL is only ever dialled: no helper, no daemon, no local child", async () => {
    let grants = 0;
    let locals = 0;
    const dialled: string[] = [];
    const connect = attachConnector({
      plan: { mode: "explicit", url: URL_OK },
      runGrant: async () => {
        grants += 1;
        return okGrant();
      },
      local: () => {
        locals += 1;
        return new FakeServer().connect();
      },
      open: (url, protocols) => {
        dialled.push([url, ...protocols].join(" "));
        const server = new FakeServer();
        const link = server.connect();
        queueMicrotask(() => server.die("refused"));
        return link;
      },
    });
    for (let i = 0; i < 3; i += 1) {
      const transport = connect(hooks());
      const closes: unknown[] = [];
      transport.onClose((info) => closes.push(info));
      await settle();
      expect(closes).toEqual([{ reason: "refused" }]);
    }
    expect(dialled).toEqual(Array(3).fill(`${URL_OK} ${GATEWAY_SUBPROTOCOL}`));
    expect(grants).toBe(0);
    expect(locals).toBe(0);
  });

  it("a terminal grant at startup runs the local child instead, visibly, from then on", async () => {
    const local = new FakeServer();
    let grants = 0;
    const connect = attachConnector({
      plan: { mode: "daemon", command: "indus", args: [] },
      runGrant: async () => {
        grants += 1;
        return terminal;
      },
      local: () => local.connect(),
    });
    const h = hooks();
    const transport = connect(h);
    const frames: unknown[] = [];
    transport.onFrame((frame) => frames.push(frame));
    await settle();
    local.ready();
    await settle();
    expect(frames).toEqual([expect.objectContaining({ method: "gateway.ready" })]);
    expect(h.events).toEqual([
      expect.objectContaining({ type: "transport.attach_failed", terminal: true, fallback: true }),
    ]);
    expect(connect.state.local).toBe(true);
    connect(hooks());
    expect(local.connections).toBe(2);
    expect(grants).toBe(1);
  });
});

// -- the app ---------------------------------------------------------------------------------------

function appRig(outcomes: GrantOutcome[], options: { local?: boolean } = {}) {
  resetStores();
  const { server, sayReady } = daemon("ok");
  for (const [method, answer] of Object.entries({
    "client.capabilities": (p: Record<string, unknown>) => ({ features: p.features ?? [] }),
    "commands.catalog": () => ({ commands: [] }),
    "config.get": (p: Record<string, unknown>) => ({ key: p.key, value: null }),
    "session.create": () => ({ session_id: "s-attached", created: true }),
    "session.resume": (p: Record<string, unknown>) => ({
      session_id: String(p.session_id),
      message_count: 0,
      last_seq: 0,
      title: null,
      messages: [],
    }),
    "session.list": () => ({ sessions: [] }),
  })) {
    server.handle(method, answer);
  }
  const local = new FakeServer();
  const grants: GrantMode[] = [];
  const clock = new FakeClock();
  const connect = attachConnector({
    plan: { mode: "daemon", command: "indus", args: [] },
    runGrant: async (mode) => {
      grants.push(mode);
      return outcomes.shift() ?? retry;
    },
    open: () => server.connect(),
    local: options.local ? () => local.connect() : null,
    clock,
  });
  const links = new LinkWatch();
  const client = new IndusClient({ connect: links.wrap(connect), replay: true });
  const runtime = new AppRuntime({ client, links, env: {}, clock, exit: () => {} });
  runtime.start();
  return { server, sayReady, local, grants, clock, runtime, client, connect };
}

const lines = (): string[] =>
  transcriptStore.getState().messages.map((m) => `${m.role}: ${m.text}`);

let running: AppRuntime | null = null;
afterEach(() => {
  running?.dispose();
  running = null;
});

describe("the app over the attach connector", () => {
  it("attaches, describes and opens a session on the daemon", async () => {
    const rig = appRig([okGrant()]);
    running = rig.runtime;
    await settle();
    rig.sayReady();
    await settle(40);
    expect(rig.grants).toEqual(["start"]);
    expect(rig.server.calls("runtime.describe")).toHaveLength(1);
    expect(rig.server.calls("client.capabilities")).toHaveLength(1);
    expect(uiStore.getState().sid).toBe("s-attached");
    expect(uiStore.getState().connected).toBe(true);
  });

  it("a terminal verdict after attaching shows the hint once and is not retried", async () => {
    const rig = appRig([okGrant(), terminal]);
    running = rig.runtime;
    await settle();
    rig.sayReady();
    await settle(40);
    rig.server.die("socket cut");
    await settle(40);
    expect(rig.grants).toEqual(["start", "recover"]);
    const said = lines().filter((line) => line.includes("cannot attach"));
    expect(said).toEqual([`system: ${attachFailureLines({ ...terminal, fallback: false })}`]);
    expect(said[0]).toContain("indus gateway restart");
    rig.clock.advance(10 * 60_000);
    await settle(40);
    expect(rig.grants).toEqual(["start", "recover"]);
    expect(rig.runtime.recovery.waiting).toBe(false);
    expect(rig.local.connections).toBe(0);
    expect(uiStore.getState().connected).toBe(false);
    expect(lines().filter((line) => line.includes(RESTART_LINE))).toHaveLength(1);
    expect(DAEMON_UNAVAILABLE_STATUS).toBe("agent daemon unavailable");
  });

  it("a retry verdict goes back to the backoff and asks again", async () => {
    const rig = appRig([okGrant(), retry, okGrant(ticket(), "inst-1")]);
    running = rig.runtime;
    await settle();
    rig.sayReady();
    await settle(40);
    rig.server.die("socket cut");
    await settle(40);
    expect(rig.grants).toEqual(["start", "recover", "recover"]);
    await settle(40);
    rig.sayReady();
    await settle(40);
    expect(rig.server.calls("session.resume").at(-1)?.params).toEqual({
      session_id: "s-attached",
    });
    expect(lines().some((line) => line.includes("cannot attach"))).toBe(false);
  });

  it("at startup a terminal verdict runs the local child with a visible line", async () => {
    const rig = appRig([terminal], { local: true });
    running = rig.runtime;
    await settle();
    rig.local.handle("client.capabilities", (p) => ({ features: p.features ?? [] }));
    rig.local.handle("commands.catalog", () => ({ commands: [] }));
    rig.local.handle("session.create", () => ({ session_id: "s-local", created: true }));
    rig.local.handle("config.get", (p) => ({ key: p.key, value: null }));
    rig.local.ready();
    await settle(40);
    expect(uiStore.getState().sid).toBe("s-local");
    expect(lines().some((line) => line.includes("Running a local agent"))).toBe(true);
    expect(rig.server.connections).toBe(0);
  });
});
