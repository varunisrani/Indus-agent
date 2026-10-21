// Electron main for the real-window security tests (security/realWindow.electron.test.ts). It
// wires the real modules the app uses — web-contents hardening, CSP header, permission policy, IPC
// registry with sender checks, the bundled preload — around a test page and the hostile guest
// pages, then writes what happened to INDUS_TEST_OUT and quits. The OS browser is never touched:
// the external opener's `openExternal` is a recorder.

import fs from "node:fs";
import path from "node:path";
import { app, BrowserWindow, ipcMain, session } from "electron";
import { fileAppOrigin } from "../../electron/appOrigin.ts";
import { IpcRegistry } from "../../electron/ipc/registry.ts";
import { buildCsp, installCspHeaders } from "../../electron/security/csp.ts";
import {
  ExternalOpener,
  installExternalOpenIpc,
  UserActivationTracker,
} from "../../electron/security/externalOpen.ts";
import { hardenWebContents } from "../../electron/security/navigation.ts";
import { installPermissionPolicy } from "../../electron/security/permissions.ts";
import { windowOptionsFor } from "../../electron/windows/factory.ts";

const out = process.env.INDUS_TEST_OUT ?? "";
const appDir = process.env.INDUS_TEST_APP_DIR ?? "";
const guestDir = process.env.INDUS_TEST_GUEST_DIR ?? "";
const preload = process.env.INDUS_TEST_PRELOAD ?? "";
app.setPath("userData", process.env.INDUS_TEST_USER_DATA ?? path.join(appDir, "user-data"));

const results = { log: [], windowsCreated: 0, externalOpens: [], guests: {} };
const log = (line) => results.log.push(line);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function finish(extra = {}) {
  fs.writeFileSync(out, JSON.stringify({ ...results, ...extra }));
  app.exit(0);
}

setTimeout(() => finish({ timedOut: true }), 30_000).unref();

app.on("browser-window-created", () => {
  results.windowsCreated += 1;
});

async function waitFor(contents, expression, timeoutMs = 5_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const value = await contents.executeJavaScript(expression).catch(() => null);
    if (value) return value;
    await sleep(50);
  }
  return null;
}

async function main() {
  await app.whenReady();
  const appOrigin = fileAppOrigin(path.join(appDir, "index.html"));
  const csp = buildCsp({ dev: false });
  installCspHeaders(session.defaultSession, appOrigin, () => csp);
  installPermissionPolicy(session.defaultSession, { appOrigin, log });
  const activation = new UserActivationTracker();
  app.on("web-contents-created", (_event, contents) => {
    hardenWebContents(contents, { appOrigin, guestPreloadPath: null, log });
    contents.on("input-event", (_e, input) => activation.noteInput(contents.id, input.type));
  });

  const known = new Set();
  const registry = new IpcRegistry({
    ipc: ipcMain,
    policy: { appOrigin, isKnownWebContents: (id) => known.has(id) },
    log,
  });
  registry.handle("indus:zoom:get", () => ({ level: 0, percent: 100 }));
  registry.handle("indus:zoom:set", ({ level }) => ({ level, percent: 100 }));
  const opener = new ExternalOpener({
    openExternal: async (url) => {
      results.externalOpens.push(url);
    },
    activation,
    log,
  });
  installExternalOpenIpc({ registry, opener, isGuestOf: () => false });
  registry.seal();

  const base = {
    preloadPath: preload,
    dev: false,
    platform: process.platform,
    title: "Indus test",
    backgroundColor: "#000000",
    titleBar: { color: "#000000", symbolColor: "#ffffff" },
  };
  const win = new BrowserWindow({
    ...windowOptionsFor("main", { ...base, bounds: { width: 800, height: 700, x: 0, y: 0 } }),
    show: true,
  });
  known.add(win.webContents.id);
  await win.loadURL(`file://${path.join(appDir, "index.html")}#/`);
  results.app = await waitFor(
    win.webContents,
    "window.__results && window.__results.done && window.__results",
  );

  // A real input event in the window, then an external open: the one allowed path.
  win.webContents.sendInputEvent({
    type: "mouseDown",
    x: 10,
    y: 10,
    button: "left",
    clickCount: 1,
  });
  win.webContents.sendInputEvent({ type: "mouseUp", x: 10, y: 10, button: "left", clickCount: 1 });
  await sleep(100);
  results.trustedOpen = await win.webContents.executeJavaScript(
    'window.indusDesktop.external.open({ url: "https://example.com/after-click" })',
  );
  results.secondOpen = await win.webContents.executeJavaScript(
    'window.indusDesktop.external.open({ url: "https://example.com/again" })',
  );

  // The same page with the same preload in a window the manager did not create: refused.
  const stranger = new BrowserWindow({
    ...windowOptionsFor("main", { ...base, bounds: { width: 400, height: 300 } }),
    show: false,
  });
  await stranger.loadURL(`file://${path.join(appDir, "index.html")}#/`);
  results.stranger = await waitFor(
    stranger.webContents,
    "window.__results && window.__results.done && window.__results",
  );
  stranger.destroy();

  // The app window tries to leave the app.
  await win.webContents
    .executeJavaScript('location.href = "https://example.invalid/away"; 1')
    .catch(() => null);
  await sleep(300);
  results.appUrlAfterNavigation = win.webContents.getURL();

  // Hostile guest pages in a helper window (no preload): nothing may open.
  for (const name of ["window-open", "synthetic-click", "file-link", "iframe-bridge"]) {
    const guest = new BrowserWindow({
      ...windowOptionsFor("hidden", { ...base, bounds: { width: 400, height: 300 } }),
      show: false,
    });
    await guest.loadURL(`file://${path.join(guestDir, `${name}.html`)}`).catch(() => null);
    results.guests[name] = await waitFor(
      guest.webContents,
      "window.__results && window.__results.done && window.__results",
    );
    guest.destroy();
  }
  finish();
}

main().catch((error) => finish({ error: String(error?.stack ?? error) }));
