import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
// @ts-expect-error -- plain JS module without types
import { renderCatalog as renderUntyped } from "../../scripts/dump-slash-catalog.mjs";
import {
  completeSlash,
  curateCatalog,
  curateCompletions,
  isHiddenCommand,
  isSlashLine,
  localCommandFor,
  OFFLINE_CATALOG,
  offlineCompletions,
  runSlash,
  type SlashHost,
} from "./slash.ts";
import { FakeConnection, rpcError } from "./testFakes.ts";

const renderCatalog = renderUntyped as (rows: unknown[]) => string;

function host(connection: FakeConnection | null, sessionId: string | null = "s1") {
  const lines: string[] = [];
  const submitted: Array<{ text: string; display: string }> = [];
  const prefilled: string[] = [];
  const local = {
    new: vi.fn(),
    model: vi.fn(),
    details: vi.fn(),
    reasoning: vi.fn(),
    help: vi.fn(),
  };
  const value: SlashHost = {
    request: connection?.request ?? null,
    sessionId,
    local,
    submit: (text, display) => submitted.push({ text, display }),
    prefill: (text) => prefilled.push(text),
    show: (text) => lines.push(text),
  };
  return { value, lines, submitted, prefilled, local };
}

describe("slash curation", () => {
  it("hides only known built-ins without a desktop surface", () => {
    expect(isHiddenCommand("/clear")).toBe(true); // terminal only
    expect(isHiddenCommand("quit")).toBe(true);
    expect(isHiddenCommand("exit")).toBe(true); // alias of a hidden one
    expect(isHiddenCommand("/sethome")).toBe(true); // messaging only
    expect(isHiddenCommand("/mouse")).toBe(true); // TUI display toggle
    expect(isHiddenCommand("/usage")).toBe(false);
    expect(isHiddenCommand("/details")).toBe(false); // desktop-owned
    expect(isHiddenCommand("/gif-search")).toBe(false); // unknown: a skill
    expect(isHiddenCommand("/my-quick-cmd")).toBe(false);
  });

  it("a new skill command appears in both completion paths", () => {
    const live = curateCompletions([
      { text: "/clear", display: "/clear" },
      { text: "/gif-search", display: "/gif-search", meta: "skill" },
      { text: "/usage", display: "/usage" },
    ]);
    expect(live.map((item) => item.text)).toEqual(["/gif-search", "/usage"]);
    const catalog = curateCatalog([
      { name: "quit", description: "x", category: "exit", kind: "builtin", runs: "client" },
      { name: "gif-search", description: "y", category: "custom", kind: "skill", runs: "server" },
    ]);
    expect(catalog.map((entry) => entry.name)).toEqual(["gif-search"]);
  });

  it("desktop-owned built-ins and their aliases resolve locally", () => {
    expect(localCommandFor("/reset")).toBe("new");
    expect(localCommandFor("model")).toBe("model");
    expect(localCommandFor("/details")).toBe("details");
    expect(localCommandFor("/usage")).toBeNull();
  });

  it("offline completion lists desktop commands and the local ones", () => {
    const items = offlineCompletions("/de").map((item) => item.text);
    expect(items).toContain("/details");
    expect(offlineCompletions("/cl").map((item) => item.text)).not.toContain("/clear");
    expect(offlineCompletions("/he").map((item) => item.text)).toContain("/help");
    expect(offlineCompletions("not slash")).toEqual([]);
  });

  it("paths are not command lines", () => {
    expect(isSlashLine("/help")).toBe(true);
    expect(isSlashLine("/usr/bin/env python")).toBe(false);
    expect(isSlashLine("/")).toBe(false);
  });
});

describe("running slash commands", () => {
  it("local built-ins never reach the daemon", async () => {
    const connection = new FakeConnection();
    const h = host(connection);
    expect(await runSlash("/reset", h.value)).toBe("local");
    expect(await runSlash("/reasoning hide", h.value)).toBe("local");
    expect(h.local.new).toHaveBeenCalledWith("");
    expect(h.local.reasoning).toHaveBeenCalledWith("hide");
    expect(connection.calls).toEqual([]);
  });

  it("a hidden built-in typed in full is refused without a call", async () => {
    const connection = new FakeConnection();
    const h = host(connection);
    expect(await runSlash("/quit", h.value)).toBe("hidden");
    expect(h.lines[0]).toContain("not available in the desktop app");
    expect(connection.calls).toEqual([]);
  });

  it("a new skill command runs: slash.exec, not owned, command.dispatch, submitted", async () => {
    const connection = new FakeConnection()
      .on("slash.exec", () => {
        throw rpcError("SLASH_NOT_OWNED", "the bridge does not run it");
      })
      .on("command.dispatch", () => ({ kind: "skill", message: "Search for a gif of a cat" }));
    const h = host(connection);
    expect(await runSlash("/gif-search cat", h.value)).toBe("submitted");
    expect(connection.calls.map((call) => call.method)).toEqual(["slash.exec", "command.dispatch"]);
    expect(connection.calls[1]?.params).toEqual({
      session_id: "s1",
      name: "gif-search",
      arg: "cat",
    });
    expect(h.submitted).toEqual([
      { text: "Search for a gif of a cat", display: "/gif-search cat" },
    ]);
  });

  it("dispatch only after a not-owned refusal", async () => {
    const connection = new FakeConnection()
      .on("slash.exec", () => ({ output: "42 calls so far" }))
      .on("command.dispatch", () => ({ kind: "exec", output: "never" }));
    const h = host(connection);
    expect(await runSlash("/usage", h.value)).toBe("output");
    expect(connection.called("command.dispatch")).toHaveLength(0);
    expect(h.lines).toEqual(["42 calls so far"]);
  });

  it("a worker timeout is shown as itself", async () => {
    const connection = new FakeConnection().on("slash.exec", () => {
      throw rpcError("SLASH_WORKER_FAILED", "the command did not answer within 30 s");
    });
    const h = host(connection);
    expect(await runSlash("/curator", h.value)).toBe("failed");
    expect(h.lines[0]).toBe(
      "/curator failed or did not answer in time: the command did not answer within 30 s",
    );
    expect(connection.called("command.dispatch")).toHaveLength(0);
  });

  it("an unknown name is reported as unknown", async () => {
    const connection = new FakeConnection()
      .on("slash.exec", () => {
        throw rpcError("SLASH_NOT_OWNED");
      })
      .on("command.dispatch", () => {
        throw rpcError("UNSUPPORTED_COMMAND");
      });
    const h = host(connection);
    expect(await runSlash("/nope", h.value)).toBe("unknown");
  });

  it("prefill and alias answers", async () => {
    const connection = new FakeConnection()
      .on("slash.exec", (params) => {
        if (params.command === "old") throw rpcError("SLASH_NOT_OWNED");
        return { prefill: "draft text" };
      })
      .on("command.dispatch", () => ({ kind: "alias", target: "/newer" }));
    const h = host(connection);
    expect(await runSlash("/old", h.value)).toBe("prefilled");
    expect(connection.called("slash.exec").map((call) => call.params.command)).toEqual([
      "old",
      "newer",
    ]);
    expect(h.prefilled).toEqual(["draft text"]);
  });

  it("server commands need a session", async () => {
    const h = host(new FakeConnection(), null);
    expect(await runSlash("/usage", h.value)).toBe("no-session");
  });

  it("late results after a session switch are dropped", async () => {
    let current = true;
    const connection = new FakeConnection().on("slash.exec", () => {
      current = false;
      return { output: "late" };
    });
    const h = host(connection);
    const outcome = await runSlash("/usage", { ...h.value, isCurrent: () => current });
    expect(outcome).toBe("stale");
    expect(h.lines).toEqual([]);
  });
});

describe("slash completion", () => {
  it("live completion is curated", async () => {
    const connection = new FakeConnection().on("complete.slash", () => ({
      items: [
        { text: "/clear", display: "/clear" },
        { text: "/codex", display: "/codex", meta: "skill" },
      ],
    }));
    const items = await completeSlash("/c", connection.request, "s1");
    expect(items.map((item) => item.text)).toEqual(["/codex"]);
  });

  it("falls back to the offline catalogue when the daemon is unreachable", async () => {
    const down = new FakeConnection().on("complete.slash", () => {
      throw new Error("not connected");
    });
    const items = await completeSlash("/us", down.request, "s1");
    expect(items.map((item) => item.text)).toContain("/usage");
    expect((await completeSlash("/us", null, null)).map((item) => item.text)).toContain("/usage");
  });
});

describe("the offline catalogue", () => {
  it("carries every registry built-in with its desktop flag", () => {
    const names = OFFLINE_CATALOG.map((command) => command.name);
    expect(names).toContain("new");
    expect(OFFLINE_CATALOG.find((command) => command.name === "clear")?.desktop).toBe(false);
    expect(OFFLINE_CATALOG.find((command) => command.name === "usage")?.desktop).toBe(true);
  });

  it("the committed JSON matches the renderer of the dump script", () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const file = path.join(here, "slashCatalog.json");
    const committed = fs.readFileSync(file, "utf8");
    const parsed = JSON.parse(committed) as { commands: unknown[] };
    expect(renderCatalog(parsed.commands)).toBe(committed);
  });

  it("the dump script --check agrees with the Python registry", () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const script = path.resolve(here, "..", "..", "scripts", "dump-slash-catalog.mjs");
    let hasUv = true;
    try {
      execFileSync("uv", ["--version"], { stdio: "ignore" });
    } catch {
      hasUv = false;
    }
    if (!hasUv) return;
    const out = execFileSync("node", [script, "--check"], { encoding: "utf8" });
    expect(out).toContain("up to date");
  }, 120_000);
});
