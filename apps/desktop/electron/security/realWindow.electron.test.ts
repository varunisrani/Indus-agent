// Real Electron window (P20-02, P20-04, P20-05): CSP blocks eval and inline script, the bridge is
// there and sender-checked, window.open and hostile guest pages open nothing, navigation away from
// the app is cancelled, and an external open needs a real input event. Skips with the reason when
// the Electron binary or a display is not available.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { buildElectron } from "../../scripts/build-electron.mjs";
import { displayWrapper, electronBinary, rootSandboxArgs } from "../../testing/electron/binary.ts";
import { buildCsp, injectCspMeta } from "./csp.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, "..", "..");
const electron = electronBinary();
const display = displayWrapper();
const skipReason =
  electron.path === null ? electron.reason : display === null ? "no display and no xvfb-run" : "";

type Results = Record<string, unknown> & {
  app: Record<string, unknown> | null;
  guests: Record<string, Record<string, unknown> | null>;
  externalOpens: string[];
  windowsCreated: number;
};

let results: Results | null = null;
let work = "";

const APP_JS = `
const r = (window.__results = {});
r.inline = window.__inline === true;
try { eval("1 + 1"); r.eval = "ran"; } catch { r.eval = "blocked"; }
try { new Function("return 1")(); r.fn = "ran"; } catch { r.fn = "blocked"; }
r.bridgeType = typeof window.indusDesktop;
r.groups = Object.keys(window.indusDesktop || {}).sort();
r.facts = window.indusDesktop && window.indusDesktop.facts;
r.open = window.open("https://example.invalid/x") === null ? "null" : "window";
(async () => {
  try { r.zoom = await window.indusDesktop.zoom.get(); } catch (e) { r.zoom = String(e.message); }
  try { await window.indusDesktop.git.status({ repo: "/x" }); r.git = "resolved"; } catch (e) { r.git = String(e.message); }
  try { await window.indusDesktop.zoom.set({ level: "big" }); r.bad = "resolved"; } catch (e) { r.bad = String(e.message); }
  try { r.untrustedOpen = await window.indusDesktop.external.open({ url: "https://example.com/no-click" }); } catch (e) { r.untrustedOpen = String(e.message); }
  r.done = true;
})();
`;

beforeAll(async () => {
  if (skipReason !== "") return;
  work = fs.mkdtempSync(path.join(os.tmpdir(), "indus-realwin-"));
  const appDir = path.join(work, "app");
  fs.mkdirSync(appDir);
  const html =
    '<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body>' +
    '<script>window.__inline = true;</script><script src="app.js"></script></body></html>';
  fs.writeFileSync(path.join(appDir, "index.html"), injectCspMeta(html, buildCsp({ dev: false })));
  fs.writeFileSync(path.join(appDir, "app.js"), APP_JS);
  const outDir = await (buildElectron as (dir: string) => Promise<string>)(
    path.join(work, "dist-electron"),
  );
  const outFile = path.join(work, "results.json");
  const args = [
    ...(display?.command ?? []),
    electron.path as string,
    ...rootSandboxArgs(),
    "--disable-gpu",
    path.join(APP_ROOT, "testing", "electron", "harness.mjs"),
  ];
  const [command, ...rest] = args;
  await new Promise<void>((resolve) => {
    const child = spawn(command as string, rest, {
      cwd: APP_ROOT,
      stdio: "ignore",
      env: {
        ...process.env,
        INDUS_TEST_OUT: outFile,
        INDUS_TEST_APP_DIR: appDir,
        INDUS_TEST_GUEST_DIR: path.join(APP_ROOT, "testing", "guestPages"),
        INDUS_TEST_PRELOAD: path.join(outDir, "preload.cjs"),
        INDUS_TEST_USER_DATA: path.join(work, "user-data"),
        ELECTRON_ENABLE_LOGGING: "",
      },
    });
    child.on("exit", () => resolve());
    child.on("error", () => resolve());
  });
  results = fs.existsSync(outFile)
    ? (JSON.parse(fs.readFileSync(outFile, "utf8")) as Results)
    : null;
}, 90_000);

afterAll(() => {
  if (work !== "") fs.rmSync(work, { recursive: true, force: true });
});

describe.skipIf(skipReason !== "")(
  `real Electron window${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it("the harness ran to the end", () => {
      expect(results).not.toBeNull();
      expect(results?.error).toBeUndefined();
      expect(results?.timedOut).toBeUndefined();
    });

    it("CSP blocks eval, new Function and the inline script; the 'self' script runs", () => {
      expect(results?.app).toMatchObject({ inline: false, eval: "blocked", fn: "blocked" });
    });

    it("the bridge is exposed with the table's groups; facts are data", () => {
      const app = results?.app as Record<string, unknown>;
      expect(app.bridgeType).toBe("object");
      expect(app.groups).toContain("zoom");
      expect(app.groups).toContain("connection");
      expect(app.facts).toMatchObject({ windowKind: "main" });
    });

    it("IPC: handled channel answers; missing handler is not-available; bad payload is bad-request", () => {
      const app = results?.app as Record<string, unknown>;
      expect(app.zoom).toEqual({ level: 0, percent: 100 });
      // P20-30 registers git: a repository outside the window's folders is refused by its guard.
      expect(String(app.git)).toMatch(/^(not-available|outside-roots):/);
      expect(String(app.bad)).toMatch(/^bad-request:/);
    });

    it("window.open from the app and every hostile guest opens nothing", () => {
      const app = results?.app as Record<string, unknown>;
      expect(app.open).toBe("null");
      const openGuest = results?.guests["window-open"] as {
        opened: Array<[string, string]>;
        late: string;
      };
      expect(openGuest.opened.every(([, handle]) => handle === "null")).toBe(true);
      expect(openGuest.late).toBe("null");
      const iframeGuest = results?.guests["iframe-bridge"] as { frame: Record<string, string> };
      const frame = iframeGuest.frame;
      expect(frame.open).toBe("null");
      expect(frame.bridge).toBe("blocked");
      expect(frame.ownBridge).toBe("undefined");
      const fileLink = results?.guests["file-link"] as { js?: string; done?: boolean };
      expect(fileLink.done).toBe(true);
      expect(fileLink.js).toBeUndefined();
      // the app window, the stranger and one helper per guest page: no popup windows
      expect(results?.windowsCreated).toBe(6);
    });

    it("a window the manager did not create is refused on every call (sender check)", () => {
      const stranger = results?.stranger as Record<string, unknown>;
      expect(String(stranger.zoom)).toMatch(/^sender-rejected:/);
      expect(String(stranger.git)).toMatch(/^sender-rejected:/);
      expect(String(stranger.untrustedOpen)).toMatch(/^sender-rejected:/);
      expect(stranger.facts).toMatchObject({ platform: "unknown" });
    });

    it("external open: refused without a real input event, allowed once after one", () => {
      const app = results?.app as Record<string, unknown>;
      expect(app.untrustedOpen).toEqual({ ok: false, reason: "no-user-activation" });
      expect(results?.trustedOpen).toEqual({ ok: true });
      expect(results?.secondOpen).toEqual({ ok: false, reason: "no-user-activation" });
      expect(results?.externalOpens).toEqual(["https://example.com/after-click"]);
    });

    it("the app window cannot navigate away from the app", () => {
      expect(String(results?.appUrlAfterNavigation)).toMatch(/^file:\/\/.*\/app\/index\.html#\/$/);
    });
  },
);
