// P20-40 ambient surfaces (fake backend): the sidebar's pop-out opens the HUD window with the
// conversation, the global shortcuts report their registration, the tray with and without a
// StatusNotifier host (a private D-Bus session bus and `testing/fakeTrayWatcher.py`), the update
// check against a loopback releases feed, and first-run onboarding with an API key that reaches
// the backend only (nothing in renderer storage).

import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { afterAll, describe, expect, it } from "vitest";
import { FakeFeed } from "../../testing/fakeFeed.ts";
import { cleanupFakes } from "../../testing/fakeIndus.ts";
import {
  APP_ROOT,
  bridge,
  clickButton,
  clickButtonStarting,
  fakeBackend,
  fillField,
  HEALTHY,
  hasButton,
  launchApp,
  type RunningApp,
  skipReason,
  sleep,
  tempDir,
  waitConnected,
} from "../fixtures.ts";

const SYSTEM_PYTHON = "/usr/bin/python3.12";
const running: RunningApp[] = [];
const helpers: ChildProcess[] = [];

afterAll(async () => {
  for (const app of running) await app.stop();
  for (const child of helpers) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  }
  cleanupFakes();
});

function withSessions(): Record<string, unknown> {
  const scenario = structuredClone(HEALTHY) as {
    rest: { routes: Record<string, unknown> };
  };
  scenario.rest.routes["GET /api/sessions"] = {
    status: 200,
    body: {
      sessions: [
        {
          id: "s-e2e-1",
          title: "Demo conversation",
          started_at: 1_790_000_000,
          last_active: 1_790_000_100,
          message_count: 2,
        },
      ],
    },
  };
  return scenario;
}

/** A private session bus (and nothing else on it): its address, and a stop. */
async function privateBus(dir: string): Promise<{ address: string; child: ChildProcess }> {
  fs.mkdirSync(dir, { recursive: true });
  const child = spawn(
    "dbus-daemon",
    ["--session", "--nofork", "--print-address=1", `--address=unix:dir=${dir}`],
    {
      stdio: ["ignore", "pipe", "ignore"],
    },
  );
  helpers.push(child);
  const lines = readline.createInterface({ input: child.stdout as NodeJS.ReadableStream });
  const address = await new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("dbus-daemon printed no address")), 10_000);
    lines.once("line", (line) => {
      clearTimeout(timer);
      resolve(line.trim());
    });
  });
  return { address, child };
}

async function trayWatcher(address: string): Promise<ChildProcess> {
  const child = spawn(SYSTEM_PYTHON, [path.join(APP_ROOT, "testing", "fakeTrayWatcher.py")], {
    stdio: ["ignore", "pipe", "ignore"],
    env: { ...process.env, DBUS_SESSION_BUS_ADDRESS: address, PYTHONPATH: "" },
  });
  helpers.push(child);
  const lines = readline.createInterface({ input: child.stdout as NodeJS.ReadableStream });
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("the tray watcher did not start")), 10_000);
    lines.on("line", (line) => {
      if (line.trim() === "ready") {
        clearTimeout(timer);
        resolve();
      }
    });
  });
  return child;
}

const trayHostSkip =
  skipReason !== ""
    ? skipReason
    : process.platform !== "linux"
      ? "the StatusNotifier host check is Linux only"
      : !fs.existsSync(SYSTEM_PYTHON)
        ? "no system Python with GObject bindings for the fake watcher"
        : "";

describe.skipIf(skipReason !== "")(
  `surfaces${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it("HUD: the sidebar's pop-out opens the conversation in a floating window", async () => {
      const work = tempDir("indus-e2e-hud-");
      const fake = fakeBackend(work, withSessions());
      const app = await launchApp({ work, home: fake.home, indus: fake.indus });
      running.push(app);
      await waitConnected(app.cdp);
      await app.cdp.waitFor(
        `document.body.innerText.includes('Demo conversation')`,
        "the sidebar row",
        30_000,
      );
      await app.cdp.eval(
        `document.querySelector('button[aria-label^="Pop Demo conversation out"]').click()`,
      );
      const hud = await app.attach(
        (t) => t.type === "page" && t.url.includes("win=hud"),
        "the HUD window",
      );
      await hud.waitFor(`location.hash === '#/s-e2e-1'`, "the HUD on the conversation");
      const state = await bridge<{ open: boolean; sessionId: string | null }>(
        app.cdp,
        `b.hud.close()`,
      );
      expect(state).toMatchObject({ ok: true, value: { open: false } });
      hud.close();
    }, 120_000);

    it("global shortcuts: every chord is reported (registered or refused, never a crash)", async () => {
      const app = running[0] as RunningApp;
      const view = await bridge<{
        shortcuts: Array<{
          action: string;
          accelerator: string;
          enabled: boolean;
          registered: boolean;
        }>;
      }>(app.cdp, `b.shortcuts.get()`);
      if (!view.ok) throw new Error(view.error);
      expect(view.value.shortcuts.map((s) => s.action).sort()).toEqual([
        "hud-snap",
        "hud-toggle",
        "quick-entry",
      ]);
      const refused = await bridge<{ rejected?: { reason: string } }>(
        app.cdp,
        `b.shortcuts.set({ action: "quick-entry", accelerator: "G" })`,
      );
      expect(refused).toMatchObject({ ok: true, value: { rejected: { reason: "no-modifier" } } });
    });

    it("tray without a StatusNotifier host: switched on, it stays unavailable and Settings says why", async () => {
      const app = running[0] as RunningApp;
      const on = await bridge(app.cdp, `b.prefs.set({ minimizeToTray: true })`);
      expect(on.ok).toBe(true);
      await app.cdp.waitFor(
        `window.indusDesktop.ambient.get().then((a) => a.tray.enabled && !a.tray.available && a.tray.reason !== null)`,
        "the tray refused for want of a host",
      );
      await app.cdp.eval(`location.hash = '#/settings?view=appearance&page=window'`);
      await app.cdp.waitFor(
        `document.querySelector('[data-row="tray"]')?.innerText.includes('Not available here')`,
        "the reason in Settings",
      );
      await app.cdp.eval(`location.hash = '#/'`);
      // closing the main window with no tray quits rather than hiding it somewhere unreachable
      const before = await app.cdp.eval<string>(`document.visibilityState`);
      expect(before).toBe("visible");
      const off = await bridge(app.cdp, `b.prefs.set({ minimizeToTray: false })`);
      expect(off.ok).toBe(true);
    });

    it.skipIf(trayHostSkip !== "")(
      `tray with a host: closing hides to the tray; the host going away brings windows back${trayHostSkip ? ` (skipped: ${trayHostSkip})` : ""}`,
      async () => {
        const work = tempDir("indus-e2e-tray-");
        const bus = await privateBus(path.join("/tmp", path.basename(work)));
        const watcher = await trayWatcher(bus.address);
        const fake = fakeBackend(work, HEALTHY);
        const app = await launchApp({
          work,
          home: fake.home,
          indus: fake.indus,
          env: { DBUS_SESSION_BUS_ADDRESS: bus.address },
        });
        running.push(app);
        await waitConnected(app.cdp);
        const on = await bridge<{ minimizeToTray: boolean }>(
          app.cdp,
          `b.prefs.set({ minimizeToTray: true })`,
        );
        expect(on).toMatchObject({ ok: true, value: { minimizeToTray: true } });
        await app.cdp.waitFor(
          `window.indusDesktop.ambient.get().then((a) => a.tray.enabled && a.tray.available)`,
          "the tray on, its host found",
          20_000,
        );
        await app.cdp.eval(`window.indusDesktop.windows.control({ action: "close" })`);
        await app.cdp.waitFor(`document.visibilityState === 'hidden'`, "the window hidden", 10_000);
        expect(app.child.exitCode).toBeNull();
        expect((await app.targets()).some((t) => t.type === "page")).toBe(true);
        watcher.kill("SIGTERM");
        await app.cdp.waitFor(
          `document.visibilityState === 'visible'`,
          "the window back after the host left",
          30_000,
        );
        expect(await app.stop()).toBe(true);
      },
      120_000,
    );

    it("update check against a loopback releases feed: the newer release is found", async () => {
      const feed = new FakeFeed();
      feed.releases = [{ tag_name: "v99.1.0", html_url: "https://example.invalid/release" }];
      const url = await feed.start();
      try {
        const work = tempDir("indus-e2e-upd-");
        const fake = fakeBackend(work, HEALTHY);
        const app = await launchApp({
          work,
          home: fake.home,
          indus: fake.indus,
          env: { INDUS_DESKTOP_UPDATE_FEED: url },
        });
        running.push(app);
        const status = await bridge<{ state: string; version?: string; owner: string }>(
          app.cdp,
          `b.updates.check()`,
        );
        expect(status).toMatchObject({ ok: true, value: { version: "99.1.0" } });
        if (status.ok) expect(["available", "manual"]).toContain(status.value.state);
        expect(feed.requests.length).toBeGreaterThan(0);
        expect(feed.requests.every((r) => r.authorization === null)).toBe(true);
        expect(await app.stop()).toBe(true);
      } finally {
        await feed.stop();
      }
    }, 120_000);

    it("first-run onboarding: an API key goes to the backend only, then the model is confirmed", async () => {
      const work = tempDir("indus-e2e-onb-");
      const scenario = structuredClone(HEALTHY) as {
        rpc: { methods: Record<string, unknown> };
        rest: { routes: Record<string, unknown> };
      };
      scenario.rest.routes["GET /api/model/options"] = {
        status: 200,
        body: {
          providers: [
            {
              name: "openrouter",
              label: "OpenRouter",
              auth_type: "api_key",
              configured: false,
              device_login: false,
            },
          ],
          current: { provider: null, model: null },
        },
      };
      scenario.rest.routes["POST /api/model/set"] = { status: 200, body: { ok: true } };
      scenario.rpc.methods["model.save_key"] = {
        result: { saved: true, models: ["router/model-a"] },
      };
      const fake = fakeBackend(work, scenario);
      const app = await launchApp({ work, home: fake.home, indus: fake.indus });
      running.push(app);
      await waitConnected(app.cdp);
      await clickButtonStarting(app.cdp, "Paste an API key", 30_000);
      const key = ["or", "e2e", "abcdefabcdefabcdef"].join("-");
      await app.cdp.waitFor(
        `!!document.querySelector('.onboarding-form input[type="password"]')`,
        "the key form",
      );
      await app.cdp.eval(`(() => {
      const select = document.querySelector('.onboarding-form select');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(select, "openrouter");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    })()`);
      await fillField(app.cdp, '.onboarding-form input[type="password"]', key);
      await app.cdp.eval(`document.querySelector('.onboarding-form').requestSubmit()`);
      await app.cdp.waitFor(hasButton("Start chatting"), "the model step", 20_000);
      await fillField(app.cdp, ".onboarding-form input[list]", "router/model-a");
      await clickButton(app.cdp, "Start chatting");
      await app.cdp.waitFor(`!(${hasButton("Start chatting")})`, "the overlay to close");
      const frames = await fake.received();
      const saved = frames.filter((f) => f.method === "model.save_key");
      expect(saved.length).toBe(1);
      expect(saved[0]?.params).toMatchObject({ slug: "openrouter", api_key: key });
      const calls = await fake.restCalls();
      expect(calls.some((c) => c.method === "POST" && c.path.startsWith("/api/model/set"))).toBe(
        true,
      );
      const stored = await app.cdp.eval<string>(
        `JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } })`,
      );
      expect(stored).not.toContain(key);
      await sleep(200);
      expect(await app.stop()).toBe(true);
    }, 120_000);
  },
);
