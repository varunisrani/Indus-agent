// Real Electron window (P24-32): a checked desktop plugin loads through `indus-plugin:` under the
// app's CSP — its imports point at the host shims, which read React and the SDK from the host
// object — while a remote URL and a blob module are blocked by the same CSP. The served texts come
// from the real `DiskPlugins` (an in-memory folder), the CSP from `buildCsp`, the scheme privileges
// from `PLUGIN_SCHEME_PRIVILEGES`. Skips with the reason when the binary or a display is missing.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { displayWrapper, electronBinary, rootSandboxArgs } from "../../testing/electron/binary.ts";
import {
  buildCsp,
  injectCspMeta,
  PLUGIN_SCHEME,
  PLUGIN_SCHEME_PRIVILEGES,
} from "../security/csp.ts";
import { DiskPlugins, HOST_KEY, type PluginFs } from "./diskPlugins.ts";

const electron = electronBinary();
const display = displayWrapper();
const skipReason =
  electron.path === null ? electron.reason : display === null ? "no display and no xvfb-run" : "";

const HOME = "/virtual/.indus";
const PLUGIN = `import { definePlugin } from "@indus/desktop-sdk";
import { createElement } from "react";
export default definePlugin({ id: "probe", register() { return createElement("b"); } });
`;

const memFs: PluginFs = {
  readdir: async (dir) => {
    if (dir === path.join(HOME, "desktop-plugins")) return ["probe.js"];
    throw new Error("ENOENT");
  },
  stat: async () => ({ size: PLUGIN.length, isFile: () => true, isDirectory: () => false }),
  readFile: async () => new TextEncoder().encode(PLUGIN),
};

const PAGE_JS = (pluginUrl: string) => `
globalThis[Symbol.for(${JSON.stringify(HOST_KEY)})] = Object.freeze({
  sdk: { AREAS: [], definePlugin: (p) => p, ui: {}, version: "1.0.0" },
  react: { createElement: () => "element" },
  jsx: { jsx: () => null, jsxs: () => null, Fragment: "f" },
});
const out = {};
try {
  const mod = await import(${JSON.stringify(pluginUrl)});
  out.plugin = mod.default.id;
  out.registerRan = mod.default.register();
} catch (error) { out.plugin = "error: " + error.message; }
try { await import("https://example.invalid/stage2.js"); out.remote = "ran"; } catch { out.remote = "blocked"; }
try {
  await import(URL.createObjectURL(new Blob(["export default 1"], { type: "text/javascript" })));
  out.blob = "ran";
} catch { out.blob = "blocked"; }
try { await import(${JSON.stringify(`${PLUGIN_SCHEME}://disk/${"0".repeat(64)}.js`)}); out.unknown = "ran"; } catch { out.unknown = "blocked"; }
document.title = "done:" + JSON.stringify(out);
`;

const MAIN_CJS = `
const { app, BrowserWindow, protocol } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const work = process.env.INDUS_PROBE_DIR;
const served = JSON.parse(fs.readFileSync(path.join(work, "served.json"), "utf8"));
protocol.registerSchemesAsPrivileged([{ scheme: ${JSON.stringify(PLUGIN_SCHEME)}, privileges: ${JSON.stringify(PLUGIN_SCHEME_PRIVILEGES)} }]);
app.whenReady().then(async () => {
  protocol.handle(${JSON.stringify(PLUGIN_SCHEME)}, async (request) => {
    const answer = served[request.url] || { status: 404, body: "" };
    return new Response(answer.body, { status: answer.status, headers: { "Content-Type": "text/javascript" } });
  });
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true } });
  const finish = (text) => { fs.writeFileSync(path.join(work, "out.txt"), text); app.quit(); };
  win.webContents.on("page-title-updated", (_e, title) => { if (title.startsWith("done:")) finish(title.slice(5)); });
  setTimeout(() => finish("timeout"), 15000);
  await win.loadFile(path.join(work, "index.html"));
});
`;

let output: Record<string, unknown> | string | null = null;
let work = "";

beforeAll(async () => {
  if (skipReason !== "") return;
  work = fs.mkdtempSync(path.join(os.tmpdir(), "indus-plugin-scheme-"));
  const disk = new DiskPlugins({ home: HOME, fs: memFs });
  const [file] = await disk.scan();
  expect(file?.status).toBe("ready");
  const urls = [
    `${PLUGIN_SCHEME}://disk/${file?.hash}.js`,
    `${PLUGIN_SCHEME}://host/sdk.js`,
    `${PLUGIN_SCHEME}://host/react.js`,
    `${PLUGIN_SCHEME}://host/jsx-runtime.js`,
  ];
  const served: Record<string, { status: number; body: string }> = {};
  for (const url of urls) served[url] = await disk.respond(url);
  fs.writeFileSync(path.join(work, "served.json"), JSON.stringify(served));
  const html =
    '<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body>' +
    '<script type="module" src="page.js"></script></body></html>';
  fs.writeFileSync(path.join(work, "index.html"), injectCspMeta(html, buildCsp({ dev: false })));
  fs.writeFileSync(path.join(work, "page.js"), PAGE_JS(urls[0] as string));
  fs.writeFileSync(path.join(work, "main.cjs"), MAIN_CJS);
  const args = [
    ...(display?.command ?? []),
    electron.path as string,
    ...rootSandboxArgs(),
    "--disable-gpu",
    path.join(work, "main.cjs"),
  ];
  const [command, ...rest] = args;
  await new Promise<void>((resolve) => {
    const child = spawn(command as string, rest, {
      stdio: "ignore",
      env: { ...process.env, INDUS_PROBE_DIR: work, ELECTRON_RUN_AS_NODE: undefined },
    });
    const timer = setTimeout(() => child.kill("SIGKILL"), 45_000);
    child.on("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
  const text = fs.existsSync(path.join(work, "out.txt"))
    ? fs.readFileSync(path.join(work, "out.txt"), "utf8")
    : "no output";
  try {
    output = JSON.parse(text) as Record<string, unknown>;
  } catch {
    output = text;
  }
}, 60_000);

afterAll(() => {
  if (work !== "") fs.rmSync(work, { recursive: true, force: true });
});

describe.skipIf(skipReason !== "")(`indus-plugin: in a real window ${skipReason}`, () => {
  it("loads a checked plugin through the host shims; remote and blob modules stay blocked", () => {
    expect(output).toEqual({
      plugin: "probe",
      registerRan: "element",
      remote: "blocked",
      blob: "blocked",
      unknown: "blocked",
    });
  });
});
