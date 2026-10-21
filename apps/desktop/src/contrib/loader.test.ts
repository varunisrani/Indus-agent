import { afterEach, describe, expect, it, vi } from "vitest";
import type { IndusPluginFile } from "../global.d.ts";
import {
  DECISIONS_KEY,
  HOST_KEY,
  installPluginHostGlobals,
  moduleUrl,
  PluginLoader,
} from "./loader.ts";
import { ContributionRegistry } from "./registry.ts";
import { definePlugin, type PluginContext, SDK_MODULE } from "./sdk.ts";
import { fakeHost } from "./testHost.ts";

const render = () => null;

function file(id: string, hash: string, extra: Partial<IndusPluginFile> = {}): IndusPluginFile {
  return {
    id,
    kind: "disk",
    file: `/home/u/.indus/desktop-plugins/${id}.js`,
    size: 10,
    hash,
    status: "ready",
    ...extra,
  };
}

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

function rig(
  modules: Record<string, unknown>,
  extra: Partial<ConstructorParameters<typeof PluginLoader>[0]> = {},
) {
  const registry = new ContributionRegistry();
  const imports: string[] = [];
  const loader = new PluginLoader({
    host: fakeHost(),
    registry,
    importModule: async (url) => {
      imports.push(url);
      if (!(url in modules)) throw new Error(`no module ${url}`);
      const found = modules[url];
      return found instanceof Promise ? await found : found;
    },
    storage: () => store,
    ...extra,
  });
  const store = memoryStorage();
  return { loader, registry, imports, store };
}

function clockModule(id: string, label: string) {
  return {
    default: definePlugin({
      id,
      register(ctx: PluginContext) {
        ctx.register({ id: "item", area: "status-bar", render, label });
      },
    }),
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("plugin loader", () => {
  it("loads, registers through the context, and reports loaded", async () => {
    const one = file("clock", "a".repeat(64));
    const { loader, registry, imports } = rig({ [moduleUrl(one)]: clockModule("clock", "v1") });
    await loader.sync([one]);
    expect(imports).toEqual([`indus-plugin://disk/${"a".repeat(64)}.js`]);
    expect(registry.list("status-bar")).toEqual([
      expect.objectContaining({ id: "clock:item", source: "plugin:clock", label: "v1" }),
    ]);
    expect(loader.inventory()).toEqual([
      expect.objectContaining({ id: "clock", status: "loaded" }),
    ]);
  });

  it("a throwing register leaves no contributions and marks an error", async () => {
    const bad = file("bad", "b".repeat(64));
    const { loader, registry } = rig({
      [moduleUrl(bad)]: {
        default: definePlugin({
          id: "bad",
          register(ctx) {
            ctx.register({ id: "one", area: "status-bar", render });
            ctx.setInterval(() => undefined, 10);
            throw new Error("register blew up");
          },
        }),
      },
    });
    await loader.sync([bad]);
    expect(registry.list("status-bar")).toEqual([]);
    expect(loader.inventory()[0]).toMatchObject({ status: "error", error: "register blew up" });
  });

  it("a changed file reloads: old contributions out, new ones in", async () => {
    const v1 = file("clock", "1".repeat(64));
    const v2 = file("clock", "2".repeat(64));
    const { loader, registry } = rig({
      [moduleUrl(v1)]: clockModule("clock", "v1"),
      [moduleUrl(v2)]: clockModule("clock", "v2"),
    });
    await loader.sync([v1]);
    await loader.sync([v2]);
    expect(registry.list("status-bar").map((c) => c.label)).toEqual(["v2"]);
    await loader.sync([]);
    expect(registry.list("status-bar")).toEqual([]);
    expect(loader.inventory()).toEqual([]);
  });

  it("refused files, wrong ids, bad shapes and slow modules are errors, not crashes", async () => {
    vi.useFakeTimers();
    const refused = file("evil", "", { status: "refused", reason: 'imports "fs"' });
    const wrong = file("wrong", "c".repeat(64));
    const shapeless = file("shape", "d".repeat(64));
    const slow = file("slow", "e".repeat(64));
    const { loader } = rig(
      {
        [moduleUrl(wrong)]: clockModule("other", "x"),
        [moduleUrl(shapeless)]: { default: { id: "shape" } },
        [moduleUrl(slow)]: new Promise(() => undefined),
      },
      { timeoutMs: 10_000 },
    );
    const done = loader.sync([refused, wrong, shapeless, slow]);
    await vi.advanceTimersByTimeAsync(10_000);
    await done;
    const byId = new Map(loader.inventory().map((r) => [r.id, r]));
    expect(byId.get("evil")).toMatchObject({ status: "refused", error: 'imports "fs"' });
    expect(byId.get("wrong")?.error).toContain("calls itself other");
    expect(byId.get("shape")?.error).toContain("register");
    expect(byId.get("slow")?.error).toContain("10000 ms");
  });

  it("disabled plugins stay in the inventory without contributions; the choice is applied live", async () => {
    const one = file("clock", "f".repeat(64));
    const off = file("opt", "9".repeat(64));
    const optIn = {
      default: definePlugin({
        id: "opt",
        defaultEnabled: false,
        register: (ctx) => {
          ctx.register({ id: "x", area: "status-bar", render });
        },
      }),
    };
    const { loader, registry, store } = rig({
      [moduleUrl(one)]: clockModule("clock", "v1"),
      [moduleUrl(off)]: optIn,
    });
    await loader.sync([one, off]);
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["clock:item"]);
    expect(loader.inventory().find((r) => r.id === "opt")?.status).toBe("disabled");
    await loader.setEnabled("clock", false, [one, off]);
    await loader.setEnabled("opt", true, [one, off]);
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["opt:x"]);
    expect(JSON.parse(store.getItem(DECISIONS_KEY) ?? "{}")).toEqual({ clock: false, opt: true });
  });

  it("a package half loads only while its agent plugin is enabled", async () => {
    const half = file("board", "7".repeat(64), { kind: "package", packageName: "board" });
    let enabled = false;
    const { loader, registry } = rig(
      { [moduleUrl(half)]: clockModule("board", "b") },
      { packageEnabled: async () => enabled },
    );
    await loader.sync([half]);
    expect(registry.list("status-bar")).toEqual([]);
    enabled = true;
    await loader.unload("board");
    await loader.sync([{ ...half }]);
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["board:item"]);
  });

  it("puts React and the SDK where the shims read them", () => {
    const target = {};
    installPluginHostGlobals(target);
    const host = (target as Record<symbol, { sdk: unknown; react: { createElement: unknown } }>)[
      Symbol.for(HOST_KEY)
    ];
    expect(host?.sdk).toBe(SDK_MODULE);
    expect(typeof host?.react.createElement).toBe("function");
  });
});
