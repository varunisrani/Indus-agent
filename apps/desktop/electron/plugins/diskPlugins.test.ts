import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DiskPlugins,
  diskPluginId,
  HOST_KEY,
  MAX_PLUGIN_BYTES,
  type PluginFs,
  RELOAD_DEBOUNCE_MS,
} from "./diskPlugins.ts";
import { classify, Kind, rewriteImports, scanPluginSource } from "./importScan.ts";
import { REACT_EXPORTS, SDK_EXPORTS } from "./sdkNames.ts";

const HOME = "/home/someone/.indus";
const DISK = path.join(HOME, "desktop-plugins");

/** An in-memory folder tree; sizes can be faked so a 16 MiB file need not be allocated. */
class MemFs implements PluginFs {
  files = new Map<string, Uint8Array>();
  fakeSizes = new Map<string, number>();
  reads: string[] = [];

  put(file: string, text: string | Uint8Array): void {
    this.files.set(file, typeof text === "string" ? new TextEncoder().encode(text) : text);
  }

  async readdir(dir: string): Promise<string[]> {
    const prefix = `${dir}/`;
    const names = new Set<string>();
    for (const file of [...this.files.keys(), ...this.fakeSizes.keys()]) {
      if (file.startsWith(prefix)) names.add(file.slice(prefix.length).split("/")[0] ?? "");
    }
    if (names.size === 0) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    return [...names];
  }

  async stat(file: string) {
    const size = this.fakeSizes.get(file) ?? this.files.get(file)?.byteLength;
    if (size === undefined) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    return { size, isFile: () => true, isDirectory: () => false };
  }

  async readFile(file: string): Promise<Uint8Array> {
    this.reads.push(file);
    const found = this.files.get(file);
    if (found === undefined) throw new Error("ENOENT");
    return found;
  }
}

const GOOD = `import { definePlugin } from "@indus/desktop-sdk";
import { createElement } from "react";
export default definePlugin({ id: "clock", register(ctx) { ctx.register({ id: "c", area: "status-bar", render: () => createElement("span", null, "12:00") }); } });
`;

function plugins(fs: MemFs, extra: Partial<ConstructorParameters<typeof DiskPlugins>[0]> = {}) {
  return new DiskPlugins({ home: HOME, fs, ...extra });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("load-time check", () => {
  it("an import of fs is refused at load", async () => {
    const fs = new MemFs();
    fs.put(path.join(DISK, "evil.js"), 'import fs from "fs";\nexport default {};\n');
    const [file] = await plugins(fs).scan();
    expect(file?.status).toBe("refused");
    expect(file?.reason).toContain('"fs"');
    expect(file?.hash).toBe("");
  });

  it.each([
    ['import x from "node:fs";', "node:fs"],
    ['import x from "./helper.js";', "./helper.js"],
    ['import "https://example.test/stage2.js";', "https://"],
    ['export * from "lodash";', "lodash"],
    ['export { a } from "../x.js";', "../x.js"],
    ['const m = await import("@indus/desktop-sdk");', "dynamic import"],
    ["const u = import.meta.url;", "import.meta"],
    ["window.indusDesktop.fs.readText({});", "indusDesktop"],
    ["globalThis.indusDesktop;", "indusDesktop"],
    ['const cp = require("child_process");', "require"],
    ["const env = process.env;", "process"],
    ['eval("1 + 1");', "eval"],
    ['const f = new Function("return 1");', "Function"],
    ["const o = {}; o.__proto__ = null;", "__proto__"],
    ["export default () => <div>hi</div>;", "JSX"],
  ])("%s is refused", (code, why) => {
    const result = scanPluginSource(code);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toContain(why);
  });

  it("words inside strings, comments, templates and regex literals do not count", () => {
    const source = [
      'import { definePlugin } from "@indus/desktop-sdk";',
      '// import fs from "fs"; window.indusDesktop',
      '/* require("x") */',
      'const label = "copy keys from \\"fs\\" and eval(1)";',
      // biome-ignore lint/suspicious/noTemplateCurlyInString: plugin source under test
      "const t = `import x from 'net' ${'nested'} process.env`;",
      'const re = /import\\("x"\\)|indusDesktop/g;',
      "const half = 10 / 2 / 1;",
      "const tag = a < b;",
      "list.process(1); obj.eval; queue.require;",
      "export default definePlugin({ id: 'ok', register() {} });",
    ].join("\n");
    const result = scanPluginSource(source);
    expect(result).toEqual({
      ok: true,
      imports: [expect.objectContaining({ specifier: "@indus/desktop-sdk" })],
    });
  });

  it("classifies a template with nested expressions and braces", () => {
    // biome-ignore lint/suspicious/noTemplateCurlyInString: plugin source under test
    const source = "const s = `a${ {b: 1}.b }c`; x";
    const kinds = classify(source);
    expect(kinds[source.indexOf("a")]).toBe(Kind.Template);
    expect(kinds[source.indexOf("b:")]).toBe(Kind.Code);
    expect(kinds[source.indexOf("c`")]).toBe(Kind.Template);
    expect(kinds[source.length - 1]).toBe(Kind.Code);
  });

  it("rewrites only the allowed specifiers to the host shims", async () => {
    const fs = new MemFs();
    fs.put(path.join(DISK, "clock.js"), GOOD);
    const disk = plugins(fs);
    const [file] = await disk.scan();
    expect(file).toMatchObject({ id: "clock", kind: "disk", status: "ready" });
    const served = disk.source(file?.hash ?? "") ?? "";
    expect(served).toContain('from "indus-plugin://host/sdk.js"');
    expect(served).toContain('from "indus-plugin://host/react.js"');
    expect(served).not.toContain('"@indus/desktop-sdk"');
    expect(
      rewriteImports(
        'import a from "react";',
        [{ specifier: "react", start: 15, end: 20 }],
        () => "X",
      ),
    ).toBe('import a from "X";');
  });
});

describe("size limit", () => {
  it("16 MiB + 1 byte is refused without reading it; exactly 16 MiB is read", async () => {
    const fs = new MemFs();
    const big = path.join(DISK, "big.js");
    const edge = path.join(DISK, "edge.js");
    fs.fakeSizes.set(big, MAX_PLUGIN_BYTES + 1);
    const exact = new Uint8Array(MAX_PLUGIN_BYTES).fill(0x20); // spaces: valid, empty program
    fs.put(edge, exact);
    const files = await plugins(fs).scan();
    const byId = new Map(files.map((f) => [f.id, f]));
    expect(byId.get("big")).toMatchObject({ status: "refused", size: MAX_PLUGIN_BYTES + 1 });
    expect(byId.get("big")?.reason).toContain(String(MAX_PLUGIN_BYTES));
    expect(fs.reads).not.toContain(big);
    expect(byId.get("edge")?.status).toBe("ready");
  });

  it("refuses text that is not UTF-8", async () => {
    const fs = new MemFs();
    fs.put(path.join(DISK, "bin.js"), new Uint8Array([0xff, 0xfe, 0x00]));
    const [file] = await plugins(fs).scan();
    expect(file?.reason).toBe("not UTF-8 text");
  });
});

describe("discovery", () => {
  it("finds disk plugins and package halves; ids are checked; duplicates refused", async () => {
    const fs = new MemFs();
    fs.put(path.join(DISK, "clock.js"), GOOD);
    fs.put(path.join(DISK, "notes.mjs"), GOOD.replace('"clock"', '"notes"'));
    fs.put(path.join(DISK, "Bad Name.js"), GOOD);
    fs.put(path.join(DISK, "readme.txt"), "x");
    fs.put(path.join(HOME, "plugins", "board", "desktop", "plugin.js"), GOOD);
    fs.put(path.join(HOME, "plugins", "clock", "desktop", "plugin.js"), GOOD);
    const files = await plugins(fs).scan();
    expect(files.map((f) => [f.id, f.kind, f.status])).toEqual([
      ["clock", "disk", "ready"],
      ["notes", "disk", "ready"],
      ["board", "package", "ready"],
      ["clock", "package", "refused"],
    ]);
    expect(files[2]?.packageName).toBe("board");
    expect(files[3]?.reason).toBe("duplicate-id");
    expect(diskPluginId("x.cjs")).toBeNull();
    expect(diskPluginId("ok-1.mjs")).toBe("ok-1");
  });

  it("no folders: an empty list", async () => {
    expect(await plugins(new MemFs()).scan()).toEqual([]);
  });
});

describe("hot reload", () => {
  it("a save reloads within one debounce; a burst of saves is one rescan", async () => {
    vi.useFakeTimers();
    const fs = new MemFs();
    fs.put(path.join(DISK, "clock.js"), GOOD);
    const changes: string[][] = [];
    const watched: { fire: (() => void) | null } = { fire: null };
    const closed = vi.fn();
    const disk = plugins(fs, {
      watch: (dir, onEvent) => {
        if (dir === DISK) watched.fire = onEvent;
        return { close: closed };
      },
      onChange: (files) => changes.push(files.map((f) => `${f.id}:${f.hash.slice(0, 8)}`)),
    });
    const [first] = await disk.scan();
    disk.watch();
    expect(watched.fire).not.toBeNull();
    fs.put(path.join(DISK, "clock.js"), GOOD.replace("12:00", "12:01"));
    watched.fire?.();
    await vi.advanceTimersByTimeAsync(RELOAD_DEBOUNCE_MS / 2);
    watched.fire?.();
    watched.fire?.();
    await vi.advanceTimersByTimeAsync(RELOAD_DEBOUNCE_MS - 1);
    expect(changes).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(changes).toHaveLength(1);
    expect(changes[0]?.[0]).not.toBe(`clock:${first?.hash.slice(0, 8)}`);
    // the old module text is gone, the new one served
    expect(disk.source(first?.hash ?? "")).toBeNull();
    // a deleted file disappears on the next rescan
    fs.files.delete(path.join(DISK, "clock.js"));
    watched.fire?.();
    await vi.advanceTimersByTimeAsync(RELOAD_DEBOUNCE_MS);
    expect(changes.at(-1)).toEqual([]);
    disk.stop();
    expect(closed).toHaveBeenCalledTimes(2);
  });
});

describe("indus-plugin: scheme", () => {
  it("serves checked modules and host shims, nothing else", async () => {
    const fs = new MemFs();
    fs.put(path.join(DISK, "clock.js"), GOOD);
    fs.put(path.join(DISK, "evil.js"), 'import "fs";');
    const disk = plugins(fs);
    const files = await disk.scan();
    const ready = files.find((f) => f.id === "clock");
    const served = await disk.respond(`indus-plugin://disk/${ready?.hash}.js`);
    expect(served.status).toBe(200);
    expect(served.body).toContain("indus-plugin://host/sdk.js");
    const sdk = await disk.respond("indus-plugin://host/sdk.js");
    expect(sdk.body).toContain(JSON.stringify(HOST_KEY));
    for (const name of SDK_EXPORTS) expect(sdk.body).toContain(name);
    const react = await disk.respond("indus-plugin://host/react.js");
    expect(react.body).toContain(REACT_EXPORTS.join(", "));
    for (const url of [
      `indus-plugin://disk/${"0".repeat(64)}.js`,
      `indus-plugin://disk/${ready?.hash}.js?x=1`,
      "indus-plugin://disk/../evil.js",
      `indus-plugin://disk/${DISK}/evil.js`,
      "indus-plugin://host/fs.js",
      "indus-plugin://other/sdk.js",
      "not a url",
    ]) {
      expect((await disk.respond(url)).status, url).toBe(404);
    }
  });
});
