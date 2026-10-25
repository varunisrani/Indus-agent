// P20-40 boot specs: first run on a clean machine (no runtime anywhere → the chooser → the in-app
// installer driven by the fake stage-protocol installer, live log, then the runtime it installed is
// found and attached), a boot that cannot succeed shows the failure overlay latched (no retry
// loop), and a healthy boot connects and announces a `gui` viewer. Fake backend only.

import fs from "node:fs";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, FAKE_ENSURE } from "../../testing/fakeIndus.ts";
import {
  APP_ROOT,
  buildApp,
  clickButtonStarting,
  fakeBackend,
  HEALTHY,
  launchApp,
  type RunningApp,
  skipReason,
  tempDir,
  waitConnected,
} from "../fixtures.ts";

const running: RunningApp[] = [];
afterAll(async () => {
  for (const app of running) await app.stop();
  cleanupFakes();
});

describe.skipIf(skipReason !== "")(`boot${skipReason ? ` (skipped: ${skipReason})` : ""}`, () => {
  it("builds the app once for the suite", async () => {
    expect(fs.existsSync(path.join(await buildApp(), "dist-electron", "main.js"))).toBe(true);
  }, 180_000);

  it("a healthy boot connects, says it is a gui viewer, and loads the sidebar list", async () => {
    const work = tempDir("indus-e2e-boot-");
    const fake = fakeBackend(work, HEALTHY);
    const app = await launchApp({ work, home: fake.home, indus: fake.indus });
    running.push(app);
    await waitConnected(app.cdp);
    const announce = (await fake.received()).find((f) => f.method === "client.capabilities");
    expect(announce?.params).toMatchObject({ surface: "gui" });
    await app.cdp.waitFor(`document.body.innerText.includes('No sessions yet')`, "the list");
    const calls = await fake.restCalls();
    expect(calls.some((c) => c.method === "GET" && c.path.startsWith("/api/sessions"))).toBe(true);
    // a configured provider: no onboarding overlay
    expect(await app.cdp.eval<boolean>(`document.body.innerText.includes('Choose later')`)).toBe(
      false,
    );
    expect(await app.stop()).toBe(true);
  }, 120_000);

  it("a boot that cannot succeed shows the failure overlay, latched (Repair, no retry loop)", async () => {
    const work = tempDir("indus-e2e-fail-");
    const fake = fakeBackend(
      work,
      JSON.parse(
        fs.readFileSync(
          path.join(APP_ROOT, "testing", "fakeBackend", "scenarios", "ensure-fails.json"),
          "utf8",
        ),
      ) as Record<string, unknown>,
    );
    const app = await launchApp({ work, home: fake.home, indus: fake.indus });
    running.push(app);
    await app.cdp.waitFor(
      `document.body.innerText.includes('Indus could not start')`,
      "the failure overlay",
      60_000,
    );
    await app.cdp.waitFor(
      `document.body.innerText.includes('Trying again by itself will not help')`,
      "the latched note",
    );
    expect(await app.cdp.eval<boolean>(`document.body.innerText.includes('Trying again in')`)).toBe(
      false,
    );
    expect(await app.stop()).toBe(true);
  }, 120_000);

  it("first run on a clean machine: chooser → in-app installer (live log) → attached", async () => {
    const work = tempDir("indus-e2e-first-");
    const home = path.join(work, "home");
    const userHome = path.join(work, "user-home");
    fs.mkdirSync(home, { recursive: true, mode: 0o700 });
    fs.mkdirSync(userHome, { recursive: true });
    const scenario = path.join(work, "scenario.json");
    fs.writeFileSync(scenario, JSON.stringify(HEALTHY));
    // The installer's "runtime" stage puts a runtime where the resolver's managed rung looks
    // (<home>/indus-agent/.venv/bin/indus); the stages themselves are the fake installer's.
    const fakeInstaller = path.join(APP_ROOT, "testing", "fakeInstaller.mjs");
    const managed = path.join(home, "indus-agent", ".venv", "bin", "indus");
    const script = path.join(work, "install.sh");
    fs.writeFileSync(
      script,
      [
        "#!/bin/sh",
        'if [ "$1" = "--stage" ] && [ "$2" = "runtime" ]; then',
        `  mkdir -p ${JSON.stringify(path.dirname(managed))}`,
        `  printf '%s\\n' '#!/bin/sh' 'INDUS_FAKE_SCENARIO=${scenario} exec ${process.execPath} ${FAKE_ENSURE} "$@"' > ${JSON.stringify(managed)}`,
        `  chmod 755 ${JSON.stringify(managed)}`,
        "fi",
        `exec ${JSON.stringify(process.execPath)} ${JSON.stringify(fakeInstaller)} "$@"`,
        "",
      ].join("\n"),
      { mode: 0o755 },
    );
    const app = await launchApp({
      work,
      home,
      indus: "",
      env: {
        HOME: userHome,
        INDUS_DESKTOP_INSTALL_SCRIPT: script,
        INDUS_FAKE_INSTALLER_STATE: path.join(work, "installer-state"),
      },
    });
    running.push(app);
    await clickButtonStarting(app.cdp, "Install on this computer", 60_000);
    await app.cdp.waitFor(
      `document.body.innerText.includes('Installing Indus')`,
      "the installer view",
      30_000,
    );
    await app.cdp.waitFor(
      `document.body.innerText.includes('Install the runtime')`,
      "the stage list",
      30_000,
    );
    await waitConnected(app.cdp, 120_000);
    expect(fs.existsSync(path.join(home, ".indus-bootstrap-complete"))).toBe(true);
    expect(fs.existsSync(managed)).toBe(true);
    expect(await app.stop()).toBe(true);
  }, 240_000);
});
