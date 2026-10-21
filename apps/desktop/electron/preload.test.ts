import fs from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildBridge, FALLBACK_FACTS, type RendererIpc } from "./ipc/bridge.ts";
import { CHANNELS } from "./ipc/channels.ts";

type Listener = (event: unknown, ...args: unknown[]) => void;

function fakeIpc(
  syncAnswer: unknown = { ok: true, value: { ...FALLBACK_FACTS, platform: "linux" } },
) {
  const listeners = new Map<string, Set<Listener>>();
  const ipc: RendererIpc & {
    emit(channel: string, ...args: unknown[]): void;
    count(channel: string): number;
  } = {
    invoke: vi.fn(async (_channel: string, payload?: unknown) => ({
      ok: true,
      value: { echoed: payload },
    })),
    send: vi.fn(),
    sendSync: vi.fn(() => syncAnswer),
    on: vi.fn((channel: string, listener: Listener) => {
      const set = listeners.get(channel) ?? new Set();
      set.add(listener);
      listeners.set(channel, set);
      return ipc;
    }),
    removeListener: vi.fn((channel: string, listener: Listener) => {
      listeners.get(channel)?.delete(listener);
      return ipc;
    }),
    emit: (channel, ...args) => {
      for (const l of listeners.get(channel) ?? []) l(args[0], ...args.slice(1));
    },
    count: (channel) => listeners.get(channel)?.size ?? 0,
  };
  return ipc;
}

/** Every value reachable from the bridge, plus whatever each function returns when called. */
async function reachable(bridge: unknown, ipc: RendererIpc): Promise<unknown[]> {
  const seen: unknown[] = [];
  const visit = async (value: unknown, depth: number): Promise<void> => {
    if (depth > 4 || seen.includes(value)) return;
    seen.push(value);
    if (typeof value === "function") {
      let result: unknown;
      try {
        result = value(() => {});
        if (result instanceof Promise) result = await result.catch(() => undefined);
      } catch {
        result = undefined;
      }
      await visit(result, depth + 1);
      return;
    }
    if (value !== null && typeof value === "object") {
      for (const key of Reflect.ownKeys(value))
        await visit((value as Record<PropertyKey, unknown>)[key], depth + 1);
    }
  };
  await visit(bridge, 0);
  expect(seen).not.toContain(ipc);
  return seen;
}

describe("bridge object", () => {
  it("nothing exposed or returned is ipcRenderer (or one of its methods)", async () => {
    const ipc = fakeIpc();
    const bridge = buildBridge(ipc);
    const seen = await reachable(bridge, ipc);
    for (const method of [ipc.invoke, ipc.send, ipc.sendSync, ipc.on, ipc.removeListener]) {
      expect(seen).not.toContain(method);
    }
  });

  it("has exactly the table's groups and methods", () => {
    const bridge = buildBridge(fakeIpc()) as Record<string, Record<string, unknown>>;
    const want = new Map<string, Set<string>>();
    for (const def of CHANNELS) {
      if (def.method === null) continue;
      want.set(def.group, (want.get(def.group) ?? new Set()).add(def.method));
    }
    expect(Object.keys(bridge).sort()).toEqual([...new Set(CHANNELS.map((c) => c.group))].sort());
    for (const [group, methods] of want) {
      expect(Object.keys(bridge[group] ?? {}).sort()).toEqual([...methods].sort());
      for (const method of methods) expect(typeof bridge[group]?.[method]).toBe("function");
    }
    expect(Object.isFrozen(bridge)).toBe(true);
  });

  it("facts are plain data read once; a refused read falls back", () => {
    const ipc = fakeIpc();
    const bridge = buildBridge(ipc) as { facts: Record<string, unknown> };
    expect(bridge.facts.platform).toBe("linux");
    expect(ipc.sendSync).toHaveBeenCalledTimes(1);
    expect(Object.values(bridge.facts).some((v) => typeof v === "function")).toBe(false);
    const refused = buildBridge(fakeIpc({ ok: false, code: "sender-rejected", message: "no" })) as {
      facts: unknown;
    };
    expect(refused.facts).toEqual(FALLBACK_FACTS);
  });

  it("a listener receives the payload only, never the event", () => {
    const ipc = fakeIpc();
    const bridge = buildBridge(ipc) as {
      zoom: { onChanged(l: (...args: unknown[]) => void): () => void };
    };
    const listener = vi.fn();
    const off = bridge.zoom.onChanged(listener);
    ipc.emit(
      "indus:zoom:changed",
      { sender: "the event object", ports: [] },
      { level: 1, percent: 120 },
    );
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls[0]).toEqual([{ level: 1, percent: 120 }]);
    off();
    off();
    expect(ipc.count("indus:zoom:changed")).toBe(0);
    expect(ipc.removeListener).toHaveBeenCalledTimes(1);
  });

  it("a throwing listener does not stop the others", async () => {
    const ipc = fakeIpc();
    const bridge = buildBridge(ipc) as { zoom: { onChanged(l: (p: unknown) => void): () => void } };
    const errors: unknown[] = [];
    const onError = (error: unknown) => errors.push(error);
    process.on("uncaughtException", onError);
    try {
      const second = vi.fn();
      bridge.zoom.onChanged(() => {
        throw new Error("listener bug");
      });
      bridge.zoom.onChanged(second);
      ipc.emit("indus:zoom:changed", {}, { level: 0, percent: 100 });
      expect(second).toHaveBeenCalledTimes(1);
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(errors).toHaveLength(1);
    } finally {
      process.off("uncaughtException", onError);
    }
  });

  it("invoke unwraps envelopes; error envelopes reject with the code; not-found is a value", async () => {
    const ipc = fakeIpc();
    const bridge = buildBridge(ipc) as {
      zoom: { get(): Promise<unknown> };
      api: { request(p: unknown): Promise<unknown> };
      windows: { control(p: unknown): void };
    };
    expect(await bridge.zoom.get()).toEqual({ echoed: undefined });
    vi.mocked(ipc.invoke).mockResolvedValueOnce({
      ok: false,
      code: "sender-rejected",
      message: "no",
    });
    await expect(bridge.zoom.get()).rejects.toThrow(/^sender-rejected: no$/);
    vi.mocked(ipc.invoke).mockResolvedValueOnce({ weird: true });
    await expect(bridge.zoom.get()).rejects.toThrow(/^internal:/);
    const notFound = { kind: "not-found", status: 404, body: { detail: "no session" } };
    vi.mocked(ipc.invoke).mockResolvedValueOnce({ ok: true, value: notFound });
    expect(await bridge.api.request({ method: "GET", path: "/api/sessions/x" })).toEqual(notFound);
    bridge.windows.control({ action: "minimize" });
    expect(ipc.send).toHaveBeenCalledWith("indus:window:control", { action: "minimize" });
  });
});

describe("preload entry", () => {
  let exposed: Array<[string, unknown]> = [];
  beforeEach(() => {
    exposed = [];
    vi.resetModules();
    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: (key: string, value: unknown) => exposed.push([key, value]),
      },
      ipcRenderer: fakeIpc(),
    }));
  });
  afterEach(() => {
    vi.doUnmock("electron");
  });

  it("exposes only window.indusDesktop", async () => {
    await import("./preload.ts");
    expect(exposed.map(([key]) => key)).toEqual(["indusDesktop"]);
  });

  it("imports nothing but electron and our pure bridge modules (sandboxed preload)", () => {
    const text = fs.readFileSync(new URL("./preload.ts", import.meta.url), "utf8");
    const imports = [...text.matchAll(/from "([^"]+)"/g)].map((m) => m[1]);
    expect(imports).toEqual(["electron", "./ipc/bridge.ts"]);
    for (const file of ["./ipc/bridge.ts", "./ipc/channels.ts", "./ipc/envelope.ts"]) {
      const source = fs.readFileSync(new URL(file, import.meta.url), "utf8");
      expect(source).not.toMatch(/from "(zod|node:[^"]+)"/);
    }
  });
});
