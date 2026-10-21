import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { HOST_KEY as MAIN_HOST_KEY } from "../../electron/plugins/diskPlugins.ts";
import { REACT_EXPORTS, SDK_EXPORTS } from "../../electron/plugins/sdkNames.ts";
import { PLUGIN_SCHEME as MAIN_SCHEME } from "../../electron/security/csp.ts";
import { HOST_KEY, PLUGIN_SCHEME } from "./loader.ts";
import {
  AREAS,
  type Contribution,
  ContributionError,
  ContributionRegistry,
  MAX_PER_SOURCE,
} from "./registry.ts";
import {
  createPluginContext,
  definePlugin,
  PluginLifetime,
  PluginShapeError,
  SDK_MODULE,
} from "./sdk.ts";
import { fakeHost } from "./testHost.ts";

const render = () => null;

function status(id: string, source = "app", order?: number): Contribution {
  return { id, area: "status-bar", source, render, ...(order === undefined ? {} : { order }) };
}

describe("contribution registry", () => {
  it("lists an area in order, then by id, and notifies once per change", () => {
    const registry = new ContributionRegistry();
    const seen = vi.fn();
    registry.subscribe(seen);
    registry.register(status("b"));
    registry.registerMany([status("c", "app", -1), status("a")]);
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["c", "a", "b"]);
    expect(seen).toHaveBeenCalledTimes(2);
    expect(registry.list("pane")).toEqual([]);
  });

  it("refuses bad contributions and duplicate ids", () => {
    const registry = new ContributionRegistry();
    registry.register(status("one"));
    expect(() => registry.register(status("one"))).toThrow(ContributionError);
    expect(() => registry.register({ id: "x", area: "nowhere" as never, source: "app" })).toThrow(
      /unknown area/,
    );
    expect(() =>
      registry.register({ id: "bad id", area: "status-bar", source: "app", render }),
    ).toThrow(/bad contribution id/);
    expect(() =>
      registry.register({ id: "p", area: "palette-command", source: "app", title: "T" }),
    ).toThrow(/needs title and run/);
    expect(() =>
      registry.register({ id: "r", area: "route", source: "app", path: "nope", render }),
    ).toThrow(/path like/);
    // all or nothing: one bad entry keeps the good one out
    expect(() => registry.registerMany([status("two"), status("one")])).toThrow();
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["one"]);
  });

  it("covers every area of the row", () => {
    expect(AREAS).toEqual([
      "pane",
      "route",
      "sidebar-nav",
      "status-bar",
      "title-bar",
      "palette-command",
      "keybind",
      "theme",
      "composer-extension",
      "session-row-decoration",
      "model-menu-decoration",
      "appearance-setting",
      "plugin-settings-page",
      "transcript-directive",
    ]);
  });

  it("a throwing register leaves no partial contributions", () => {
    const registry = new ContributionRegistry();
    const seen = vi.fn();
    registry.subscribe(seen);
    expect(() =>
      registry.transaction("plugin:x", (stage) => {
        stage.add(status("x:1", "plugin:x"));
        stage.add(status("x:2", "plugin:x"));
        throw new Error("boom in register");
      }),
    ).toThrow("boom in register");
    expect(registry.list("status-bar")).toEqual([]);
    expect(seen).not.toHaveBeenCalled();
    // and after it, the same ids can be registered (nothing half-held)
    registry.transaction("plugin:x", (stage) => {
      stage.add(status("x:1", "plugin:x"));
    });
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["x:1"]);
  });

  it("commits a transaction in one change; its disposer drops all of it", () => {
    const registry = new ContributionRegistry();
    const seen = vi.fn();
    registry.subscribe(seen);
    const dispose = registry.transaction("plugin:y", (stage) => {
      stage.add(status("y:1", "plugin:y"));
      const undo = stage.add(status("y:2", "plugin:y"));
      stage.add(status("y:3", "plugin:y"));
      undo(); // withdrawn while staging: never committed
    });
    expect(seen).toHaveBeenCalledTimes(1);
    expect(registry.list("status-bar").map((c) => c.id)).toEqual(["y:1", "y:3"]);
    dispose();
    dispose();
    expect(registry.list("status-bar")).toEqual([]);
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("bounds the contributions of one source", () => {
    const registry = new ContributionRegistry();
    expect(() =>
      registry.transaction("plugin:z", (stage) => {
        for (let i = 0; i <= MAX_PER_SOURCE; i += 1) stage.add(status(`z:${i}`, "plugin:z"));
      }),
    ).toThrow(new RegExp(String(MAX_PER_SOURCE)));
    expect(registry.countOf("plugin:z")).toBe(0);
    expect(() =>
      registry.transaction("plugin:z", (stage) => stage.add(status("w:1", "plugin:w"))),
    ).toThrow(/another source/);
  });

  it("a subscriber that throws does not stop the others", () => {
    const registry = new ContributionRegistry();
    const good = vi.fn();
    registry.subscribe(() => {
      throw new Error("bad");
    });
    registry.subscribe(good);
    registry.register(status("a"));
    expect(good).toHaveBeenCalledOnce();
  });
});

describe("plugin context", () => {
  function contextFor(registry = new ContributionRegistry(), host = fakeHost()) {
    const lifetime = new PluginLifetime();
    const ctx = createPluginContext({
      pluginId: "demo",
      host,
      registry,
      lifetime,
      stage: () => null,
    });
    return { ctx, lifetime, registry, host };
  }

  it("stamps the source and namespaces ids", () => {
    const { ctx, registry } = contextFor();
    ctx.register({ id: "clock", area: "status-bar", render });
    expect(registry.list("status-bar")).toEqual([
      expect.objectContaining({ id: "demo:clock", source: "plugin:demo" }),
    ]);
  });

  it("ending the lifetime undoes contributions, timers and listeners; one bad cleanup does not stop the rest", () => {
    vi.useFakeTimers();
    const { ctx, lifetime, registry } = contextFor();
    const fired = vi.fn();
    ctx.register({ id: "a", area: "status-bar", render });
    ctx.setInterval(fired, 100);
    const target = new EventTarget();
    const heard = vi.fn();
    ctx.addEventListener(target, "ping", heard);
    ctx.onDispose(() => {
      throw new Error("bad cleanup");
    });
    const errors: unknown[] = [];
    lifetime.end((e) => errors.push(e));
    vi.advanceTimersByTime(500);
    target.dispatchEvent(new Event("ping"));
    expect(fired).not.toHaveBeenCalled();
    expect(heard).not.toHaveBeenCalled();
    expect(registry.list("status-bar")).toEqual([]);
    expect(errors).toHaveLength(1);
    vi.useRealTimers();
  });

  it("curated doors: rpc allow-list, own REST namespace, app paths only", async () => {
    const { ctx, host } = contextFor();
    await ctx.rpc("session.list");
    await expect(ctx.rpc("prompt.submit", {})).rejects.toThrow(PluginShapeError);
    await expect(ctx.rpc("shell.exec", {})).rejects.toThrow(/not open to plugins/);
    expect(host.rpcCalls).toEqual(["session.list"]);
    await ctx.rest("/items");
    expect(host.restCalls).toEqual(["/api/ext/demo/items"]);
    await expect(async () => ctx.rest("/../../api/env")).rejects.toThrow();
    expect(() => ctx.rest("https://elsewhere.test/x")).toThrow();
    ctx.navigate("/settings?view=about");
    expect(() => ctx.navigate("https://evil.test/")).toThrow();
    expect(() => ctx.navigate("//evil.test/")).toThrow();
    expect(host.navigated).toEqual(["/settings?view=about"]);
  });

  it("the context holds no bridge", () => {
    const { ctx } = contextFor();
    const reachable = JSON.stringify(Object.keys(ctx));
    expect(reachable).not.toContain("indusDesktop");
    for (const value of Object.values(ctx)) {
      expect(value).not.toBe((globalThis as { indusDesktop?: unknown }).indusDesktop ?? Symbol());
    }
  });

  it("storage is namespaced per plugin", () => {
    const host = fakeHost();
    const a = contextFor(new ContributionRegistry(), host).ctx;
    a.storage.set("k", { n: 1 });
    expect(a.storage.get("k", null)).toEqual({ n: 1 });
    expect(host.storage()?.getItem("indus.plugin.demo.k")).toBe('{"n":1}');
    a.storage.remove("k");
    expect(a.storage.get("k", "gone")).toBe("gone");
  });
});

describe("sdk module", () => {
  it("definePlugin checks the shape", () => {
    expect(() => definePlugin({ id: "Bad", register: () => undefined })).toThrow(PluginShapeError);
    expect(() => definePlugin({ id: "ok" } as never)).toThrow(/register/);
    const plugin = definePlugin({ id: "ok", register: () => undefined });
    expect(plugin.id).toBe("ok");
  });

  it("exports exactly what main's shim re-exports; React has every shimmed name", () => {
    expect(Object.keys(SDK_MODULE).sort()).toEqual([...SDK_EXPORTS].sort());
    for (const name of REACT_EXPORTS) expect(name in React, name).toBe(true);
  });

  it("main and the renderer agree on the host key and the scheme", () => {
    expect(HOST_KEY).toBe(MAIN_HOST_KEY);
    expect(PLUGIN_SCHEME).toBe(MAIN_SCHEME);
  });
});
