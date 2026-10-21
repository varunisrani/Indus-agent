// P20-40 packaged smoke (P20-38's build, end to end on this host): an unsigned `--dir` package
// made offline from the locally downloaded Electron (`INDUS_DESKTOP_ELECTRON_DIST`) unless CI's
// desktop-package job already built one; the fuse verifier and the archive check over it; then the
// packaged app itself is started against the fake backend and must be interactive (connected,
// composer ready) within 60 s, with its CSP refusing an injected inline script. Writes only under
// the gitignored `build/` folder and removes what it built.

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { displayWrapper, rootSandboxArgs } from "../../testing/electron/binary.ts";
import { appPage, Cdp } from "../../testing/electron/cdp.ts";
import { cleanupFakes } from "../../testing/fakeIndus.ts";
import {
  APP_ROOT,
  CONNECTED,
  cleanEnv,
  ELECTRON_PATH,
  skipReason as e2eSkip,
  fakeBackend,
  freePort,
  HEALTHY,
  tempDir,
} from "../fixtures.ts";

const BUILD = path.join(APP_ROOT, "build");
const RELEASE = path.join(BUILD, "release");
const prebuilt = fs.existsSync(path.join(RELEASE, "linux-unpacked", "indus-desktop"));
// the build writes the bundles into the checkout too (gitignored); leave none behind that it made
const OUTPUTS = ["dist", "dist-electron"].map((name) => path.join(APP_ROOT, name));
const preexisting = new Set(OUTPUTS.filter((dir) => fs.existsSync(dir)));
const skipReason =
  e2eSkip !== ""
    ? e2eSkip
    : process.platform !== "linux"
      ? "the packaged smoke drives the Linux build here (macOS/Windows: CI's desktop-package job)"
      : "";

afterAll(() => {
  cleanupFakes();
  if (skipReason === "" && !prebuilt) {
    fs.rmSync(BUILD, { recursive: true, force: true });
    for (const dir of OUTPUTS)
      if (!preexisting.has(dir)) fs.rmSync(dir, { recursive: true, force: true });
  }
});

describe.skipIf(skipReason !== "")(
  `unsigned --dir package${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it("packs (or reuses CI's build), flips the fuses and passes the archive check", () => {
      if (!prebuilt) {
        const run = spawnSync(process.execPath, ["scripts/dist-unsigned.mjs", "--dir"], {
          cwd: APP_ROOT,
          env: {
            ...process.env,
            INDUS_DESKTOP_ELECTRON_DIST: path.dirname(ELECTRON_PATH ?? ""),
            CSC_IDENTITY_AUTO_DISCOVERY: "false",
          },
          encoding: "utf8",
          timeout: 300_000,
        });
        expect(run.status, `${run.stdout}\n${run.stderr}`).toBe(0);
        expect(run.stdout).toContain("[fuses] set on");
        expect(run.stdout).toContain("[asar]");
      }
      const fuses = spawnSync(process.execPath, ["scripts/verify-fuses.mjs", RELEASE], {
        cwd: APP_ROOT,
        encoding: "utf8",
      });
      expect(fuses.status, fuses.stdout + fuses.stderr).toBe(0);
      const asar = spawnSync(process.execPath, ["scripts/check-asar.mjs", RELEASE], {
        cwd: APP_ROOT,
        encoding: "utf8",
      });
      expect(asar.status, asar.stdout + asar.stderr).toBe(0);
    }, 360_000);

    it("the packaged app is interactive within 60 s and its CSP refuses an inline script", async () => {
      const binary = path.join(RELEASE, "linux-unpacked", "indus-desktop");
      expect(fs.existsSync(binary)).toBe(true);
      const work = tempDir("indus-e2e-pack-");
      const fake = fakeBackend(work, HEALTHY);
      const port = await freePort();
      const display = displayWrapper();
      const [command, ...args] = [
        ...(display?.command ?? []),
        binary,
        ...rootSandboxArgs(),
        "--disable-gpu",
        `--remote-debugging-port=${port}`,
      ];
      const started = Date.now();
      const child = spawn(command as string, args, {
        cwd: work,
        detached: true,
        stdio: "ignore",
        env: cleanEnv({
          HOME: path.join(work, "user-home"),
          INDUS_HOME: fake.home,
          INDUS_DESKTOP_INDUS: fake.indus,
          INDUS_DESKTOP_USER_DATA_DIR: path.join(work, "user-data"),
        }),
      });
      const gone = new Promise<void>((resolve) => child.once("exit", () => resolve()));
      let cdp: Cdp | null = null;
      try {
        cdp = await Cdp.open(await appPage(port, 60_000));
        await cdp.send("Runtime.enable");
        await cdp.waitFor(CONNECTED, "the packaged app's connection", 60_000);
        await cdp.waitFor(`!!document.querySelector('textarea')`, "the composer", 10_000);
        expect(Date.now() - started).toBeLessThanOrEqual(60_000);
        expect(
          await cdp.eval<boolean>(`!!window.indusDesktop && window.indusDesktop.facts.packaged`),
        ).toBe(true);
        const csp = await cdp.eval<{
          ran: boolean;
          violation: string | null;
        }>(`new Promise((resolve) => {
          let violation = null;
          document.addEventListener("securitypolicyviolation", (e) => { violation = e.violatedDirective; }, { once: true });
          const script = document.createElement("script");
          script.textContent = "window.__inlineRan = true";
          document.head.append(script);
          setTimeout(() => resolve({ ran: window.__inlineRan === true, violation }), 200);
        })`);
        expect(csp.ran).toBe(false);
        expect(csp.violation ?? "").toMatch(/^script-src/);
      } finally {
        cdp?.close();
        if (child.pid !== undefined && child.exitCode === null) {
          try {
            process.kill(-child.pid, "SIGTERM");
          } catch {
            // gone
          }
          await Promise.race([gone, new Promise((resolve) => setTimeout(resolve, 10_000))]);
        }
      }
    }, 120_000);
  },
);
