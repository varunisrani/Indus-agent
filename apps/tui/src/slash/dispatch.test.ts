import { FakeRpcError } from "@indus/client/testing";
import { ERROR_CODES } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import { uiStore } from "../state/uiStore.ts";
import { createDispatcher, matchCatalog, parseCommand } from "./dispatch.ts";
import { entry, slashSetup } from "./testContext.ts";

const fail = (name: keyof typeof ERROR_CODES, message: string) =>
  new FakeRpcError(ERROR_CODES[name].code, message);

describe("parsing", () => {
  it("splits the word and the rest", () => {
    expect(parseCommand("/deploy  prod --fast ")).toEqual({ name: "deploy", arg: "prod --fast" });
    expect(parseCommand("/help")).toEqual({ name: "help", arg: "" });
  });
});

describe("catalogue matching", () => {
  const catalog = [
    entry("deploy", { aliases: ["ship"] }),
    entry("diff"),
    entry("zeta", { description: "deploys" }),
  ];

  it("exact aliases and names win", () => {
    expect(matchCatalog("SHIP", catalog)).toEqual({ type: "exact", canonical: "deploy" });
    expect(matchCatalog("diff", catalog)).toEqual({ type: "exact", canonical: "diff" });
  });

  it("one prefix match runs; several are ambiguous; descriptions never count", () => {
    expect(matchCatalog("dep", catalog)).toEqual({ type: "match", canonical: "deploy" });
    expect(matchCatalog("d", catalog)).toEqual({ type: "ambiguous", names: ["deploy", "diff"] });
    expect(matchCatalog("deploys", catalog)).toEqual({ type: "none" });
  });

  it("entries the server marks unavailable never match (W48-h)", () => {
    const listed = [entry("gs", { runs: "unavailable" }), entry("git-log")];
    expect(matchCatalog("g", listed)).toEqual({ type: "match", canonical: "git-log" });
    expect(matchCatalog("gs", listed)).toEqual({ type: "none" });
  });

  it("a prefix match beats substring matches", () => {
    expect(matchCatalog("ze", [entry("zeta"), entry("amaze")])).toEqual({
      type: "match",
      canonical: "zeta",
    });
    expect(matchCatalog("et", [entry("zeta"), entry("beta")])).toEqual({
      type: "ambiguous",
      names: ["zeta", "beta"],
    });
  });
});

describe("dispatch through the bridge", () => {
  it("a worker failure is shown as itself, never as unknown, and never falls through", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", () => {
      throw fail("SLASH_WORKER_FAILED", "the handler did not answer within 45 s");
    });
    await createDispatcher(ctx).run("/archive");
    expect(out.lines).toEqual(["/archive failed: the handler did not answer within 45 s"]);
    expect(server.calls("command.dispatch")).toHaveLength(0);
  });

  it("a client-side timeout is shown as a timeout", async () => {
    const { server, ctx, out } = slashSetup({ timeoutMs: 20 });
    server.handle("slash.exec", () => new Promise(() => {}));
    await createDispatcher(ctx).run("/archive");
    expect(out.lines[0]).toMatch(/^\/archive failed: slash\.exec: no answer within 20 ms/);
    expect(out.lines.join()).not.toMatch(/unknown/i);
    expect(server.calls("command.dispatch")).toHaveLength(0);
  });

  it("only a not-owned refusal falls through to command.dispatch", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", () => {
      throw fail("SLASH_NOT_OWNED", "the client runs this one");
    });
    server.handle("command.dispatch", (params) => ({
      kind: "exec",
      output: `ran ${String(params.name)}`,
    }));
    await createDispatcher(ctx).run("/greet bob");
    expect(server.calls("command.dispatch")[0]?.params).toEqual({
      session_id: "s-1",
      name: "greet",
      arg: "bob",
    });
    expect(out.lines).toEqual(["ran greet"]);
  });

  it("not owned and not dispatchable shows the first refusal", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", () => {
      throw fail("SLASH_NOT_OWNED", "arrives with P13");
    });
    server.handle("command.dispatch", () => {
      throw fail("UNSUPPORTED_COMMAND", "not a command");
    });
    await createDispatcher(ctx).run("/plugins");
    expect(out.lines).toEqual(["/plugins: arrives with P13"]);
  });

  it("an unknown command says so", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", () => {
      throw fail("UNSUPPORTED_COMMAND", "no such command");
    });
    await createDispatcher(ctx).run("/nope");
    expect(out.lines[0]).toContain("Unknown command: /nope");
  });

  it("a skill submits the body while the transcript shows the invocation", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", () => {
      throw fail("SLASH_NOT_OWNED", "skill");
    });
    server.handle("command.dispatch", () => ({ kind: "skill", message: "LONG SKILL BODY" }));
    await createDispatcher(ctx).run("/release v2");
    expect(out.submits).toEqual([{ body: "LONG SKILL BODY", shown: "/release v2" }]);
  });

  it("a send result shows its notice, then submits", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", () => {
      throw fail("SLASH_NOT_OWNED", "quick");
    });
    server.handle("command.dispatch", () => ({
      kind: "send",
      message: "hello",
      notice: "sending the greeting",
    }));
    await createDispatcher(ctx).run("/hi");
    expect(out.lines).toEqual(["sending the greeting"]);
    expect(out.submits).toEqual([{ body: "hello", shown: "/hi" }]);
  });

  it("an alias result runs its target", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", (params) => {
      if (params.command === "tl") throw fail("SLASH_NOT_OWNED", "alias");
      return { output: `ran ${String(params.command)}` };
    });
    server.handle("command.dispatch", () => ({ kind: "alias", target: "tools" }));
    await createDispatcher(ctx).run("/tl");
    expect(out.lines).toEqual(["ran tools"]);
  });

  it("exec results: long output pages, prefill fills the composer, submit sends", async () => {
    const { server, ctx, out } = slashSetup();
    server.handle("slash.exec", (params) => {
      if (params.command === "logbook") return { output: "entry\n".repeat(5) };
      if (params.command === "rewind") return { prefill: "edit me" };
      if (params.command === "short") return { output: "one line" };
      return { submit: "go again" };
    });
    const dispatcher = createDispatcher(ctx);
    await dispatcher.run("/logbook");
    await dispatcher.run("/rewind");
    await dispatcher.run("/again");
    await dispatcher.run("/short");
    expect(out.pages).toEqual([{ text: "entry\n".repeat(5), title: "/logbook" }]);
    expect(out.prefills).toEqual(["edit me"]);
    expect(out.submits).toEqual([{ body: "go again", shown: undefined }]);
    expect(out.lines).toEqual(["one line"]);
  });

  it("drops a late result after /new changed the session", async () => {
    const { server, ctx, out } = slashSetup();
    let release: (value: unknown) => void = () => {};
    server.handle("slash.exec", () => new Promise((resolve) => (release = resolve)));
    const pending = createDispatcher(ctx).run("/remote-status");
    await new Promise((resolve) => setTimeout(resolve, 5));
    uiStore.getState().patch({ sid: "s-2", storedSid: "s-2", revision: 1 });
    release({ output: "late output" });
    await pending;
    expect(out.lines).toEqual([]);
    expect(out.pages).toEqual([]);
  });

  it("drops a late result once a newer command ran", async () => {
    const { server, ctx, out } = slashSetup();
    const releases: ((value: unknown) => void)[] = [];
    server.handle("slash.exec", (params) =>
      params.command === "slow"
        ? new Promise((resolve) => releases.push(resolve))
        : { output: "fast" },
    );
    const dispatcher = createDispatcher(ctx);
    const slow = dispatcher.run("/slow");
    await new Promise((resolve) => setTimeout(resolve, 5));
    await dispatcher.run("/quick");
    releases[0]?.({ output: "slow output" });
    await slow;
    expect(out.lines).toEqual(["fast"]);
  });

  it("no session means no call", async () => {
    const { server, ctx, out } = slashSetup();
    uiStore.getState().patch({ sid: null });
    await createDispatcher(ctx).run("/archive");
    expect(out.lines[0]).toContain("needs a session");
    expect(server.calls("slash.exec")).toHaveLength(0);
  });
});

describe("catalogue routing", () => {
  const many = ["stash", "stats", "stamp", "start", "staff", "stage", "stack", "stair"].map((n) =>
    entry(n),
  );

  it("an ambiguous word lists six names", async () => {
    const { server, ctx, out } = slashSetup({ catalog: many });
    await createDispatcher(ctx).run("/sta");
    expect(out.lines).toEqual([
      "/sta is ambiguous: /stash, /stats, /stamp, /start, /staff, /stage, …",
    ]);
    expect(server.received).toHaveLength(0);
  });

  it("an exact alias and a unique prefix run the canonical command", async () => {
    const { server, ctx } = slashSetup({ catalog: [entry("deploy", { aliases: ["ship"] })] });
    server.handle("slash.exec", () => ({ output: "ok" }));
    const dispatcher = createDispatcher(ctx);
    await dispatcher.run("/ship prod");
    await dispatcher.run("/depl prod");
    expect(server.calls("slash.exec").map((f) => f.params)).toEqual([
      { session_id: "s-1", command: "deploy prod" },
      { session_id: "s-1", command: "deploy prod" },
    ]);
  });

  it("a catalogue alias of a local command runs locally", async () => {
    const { ctx, out } = slashSetup({
      catalog: [entry("quit", { aliases: ["bye"], runs: "client" })],
    });
    await createDispatcher(ctx).run("/bye");
    expect(out.exits).toEqual([0]);
  });

  it("local names and aliases match in any case", async () => {
    const { ctx, out } = slashSetup();
    await createDispatcher(ctx).run("/EXIT");
    expect(out.exits).toEqual([0]);
  });
});
