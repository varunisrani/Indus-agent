// The Electron main process (P20-01). The order below is the contract:
//
//   pre-ready   env defaults → userData override → desktop.log → launch switches from
//               config.yaml → ozone → privileged scheme → single-instance lock (+ deep-link client)
//   loser       hands its argv to the holder and exits; nothing below runs (R08.3)
//   on ready    login-shell PATH (bounded, never blocking) → session security (CSP header,
//               permissions, web-contents hardening) → IPC registry → install steps → seal →
//               app menu → main window
//
// Later tasks add exactly one `install…(context)` line to the INSTALL STEPS list in `boot()`.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  app,
  BrowserWindow,
  type BrowserWindowConstructorOptions,
  ipcMain,
  Menu,
  type MenuItemConstructorOptions,
  protocol,
  safeStorage,
  screen,
  session,
  shell,
  type WebContents,
} from "electron";
import { appMenuTemplate } from "./appMenu.ts";
import { type AppOrigin, devServerOrigin, fileAppOrigin } from "./appOrigin.ts";
import type { MainContext } from "./context.ts";
import { applyEnvDefaults, parseBakedEnvDefaults } from "./envDefaults.ts";
import { type IpcMainLike, IpcRegistry } from "./ipc/registry.ts";
import { chooseOzonePlatform, planSwitches, readLaunchConfig } from "./launchConfig.ts";
import { DesktopLog } from "./log.ts";
import { desktopPaths, resolveIndusHome, userDataOverride } from "./paths.ts";
import {
  DesktopSecrets,
  installSecretsIpc,
  probeKeychain,
  type SafeStorageLike,
} from "./secrets.ts";
import { CspState, installCspHeaders, MEDIA_SCHEME } from "./security/csp.ts";
import {
  ExternalOpener,
  installExternalOpenIpc,
  UserActivationTracker,
} from "./security/externalOpen.ts";
import {
  type GuardedContents,
  hardenWebContents,
  PREVIEW_PARTITION,
} from "./security/navigation.ts";
import {
  installDenyAllPermissions,
  installPermissionPolicy,
  type PermissionSession,
} from "./security/permissions.ts";
import { mergeLoginShellPath } from "./shellPath.ts";
import { acquireSingleInstance, type LockApp, lockTempDir } from "./singleInstance.ts";
import { installWindowIpc } from "./windows/ipc.ts";
import { type BrowserWindowLike, WindowManager } from "./windows/manager.ts";

/** Replaced at build time by scripts/build-electron.mjs with the validated defaults JSON. */
declare const __INDUS_ENV_DEFAULTS__: string | undefined;

const APP_NAME = "Indus";
const HERE = import.meta.dirname;

// -- pre-ready ------------------------------------------------------------------------------------

applyEnvDefaults(
  process.env,
  parseBakedEnvDefaults(
    typeof __INDUS_ENV_DEFAULTS__ === "string" ? __INDUS_ENV_DEFAULTS__ : undefined,
  ),
);
const userDataDir = userDataOverride(process.env);
if (userDataDir !== null) app.setPath("userData", userDataDir);
app.setName(APP_NAME);

const dev = !app.isPackaged;
const paths = desktopPaths(resolveIndusHome(process.env, os.homedir()), app.getPath("userData"));
const log = new DesktopLog({
  file: paths.desktopLog,
  ...(dev ? { echo: (line: string) => process.stderr.write(`${line}\n`) } : {}),
});

{
  let yaml = "";
  try {
    yaml = fs.readFileSync(paths.homeConfig, "utf8");
  } catch {
    // first run: no config yet
  }
  const { config, warnings } = readLaunchConfig(yaml);
  for (const warning of warnings) log.write(`[launch] ${warning}`);
  for (const planned of planSwitches(config, process.argv.slice(1))) {
    if (planned.value === undefined) app.commandLine.appendSwitch(planned.name);
    else app.commandLine.appendSwitch(planned.name, planned.value);
    log.write(`[launch] switch --${planned.name} from config.yaml`);
  }
  const ozone = chooseOzonePlatform({
    platform: process.platform,
    env: process.env,
    argv: process.argv,
  });
  if (ozone !== null) app.commandLine.appendSwitch("ozone-platform", ozone);
}

protocol.registerSchemesAsPrivileged([
  {
    scheme: MEDIA_SCHEME,
    privileges: { secure: true, standard: true, stream: true, supportFetchAPI: true },
  },
]);

let latestDeepLink: string | null = null;
let windowsRef: WindowManager | null = null;

/** A live process that is not a zombie (Linux reads its state from /proc). */
function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    return stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3) !== "Z";
  } catch {
    return true;
  }
}

// Chromium puts the lock's socket under TMPDIR; a TMPDIR too long for a socket path would abort
// the launch, so the lock alone uses a short one and the rest of the app keeps the user's.
const callerTmp = process.env.TMPDIR;
const shortTmp = lockTempDir(process.env, process.platform);
if (shortTmp !== null) process.env.TMPDIR = shortTmp;
const instance = acquireSingleInstance({
  app: app as unknown as LockApp,
  platform: process.platform,
  dev,
  execPath: process.execPath,
  ...(dev && process.argv[1] !== undefined ? { appPath: path.resolve(process.argv[1]) } : {}),
  registerProtocol: process.env.INDUS_DESKTOP_SKIP_PROTOCOL_REGISTRATION !== "1",
  log: (line) => log.write(line),
  onSecondLaunch: (launch) => {
    log.write(`[lock] second launch (deep link: ${launch.deepLink === null ? "no" : "yes"})`);
    if (launch.deepLink !== null) latestDeepLink = launch.deepLink; // P20-34 delivers it
    const main = windowsRef?.mainWindow() ?? null;
    if (main !== null) windowsRef?.raise(main, "second-launch");
    else if (app.isReady()) windowsRef?.openMain();
  },
  ...(process.platform === "linux"
    ? {
        staleLock: {
          file: path.join(app.getPath("userData"), "SingletonLock"),
          probe: {
            fs: {
              readlink: (file: string) => fs.readlinkSync(file),
              unlink: (file: string) => fs.unlinkSync(file),
            },
            hostname: os.hostname(),
            isAlive: processAlive,
          },
        },
      }
    : {}),
});
if (shortTmp !== null) {
  if (callerTmp === undefined) delete process.env.TMPDIR;
  else process.env.TMPDIR = callerTmp;
}

if (!instance.holder) {
  log.close();
  app.exit(0);
} else {
  // A launcher or test harness stopping the app by signal gets an orderly quit (window state and
  // the log are written); anything still buffered is written synchronously at exit.
  for (const signal of ["SIGTERM", "SIGINT"] as const) process.once(signal, () => app.quit());
  process.once("exit", () => log.flush());
  app.on("open-url", (event, url) => {
    event.preventDefault();
    if (url.toLowerCase().startsWith(`${instance.scheme}://`)) latestDeepLink = url; // P20-34
  });
  boot().catch((error: unknown) => {
    log.write(`[boot] failed: ${error instanceof Error ? error.message : String(error)}`);
  });
}

// -- ready ----------------------------------------------------------------------------------------

function resolveAppOrigin(): { origin: AppOrigin; devServer: string | null } {
  const devServer = dev ? devServerOrigin(process.env.INDUS_DESKTOP_DEV_SERVER) : null;
  if (devServer !== null) return { origin: { kind: "dev", origin: devServer }, devServer };
  return {
    origin: fileAppOrigin(path.join(app.getAppPath(), "dist", "index.html")),
    devServer: null,
  };
}

async function boot(): Promise<void> {
  await app.whenReady();
  // Never awaited here (boot must not wait on a shell profile); P20-08 awaits it before it
  // resolves or spawns anything (R08.16).
  const shellPath = mergeLoginShellPath({ env: process.env, platform: process.platform }).then(
    (outcome) => {
      log.write(
        outcome.applied ? "[env] login-shell PATH merged" : `[env] PATH kept (${outcome.reason})`,
      );
      return outcome;
    },
  );

  const { origin: appOrigin, devServer } = resolveAppOrigin();
  const csp = new CspState({ dev, ...(devServer === null ? {} : { devServerOrigin: devServer }) });
  installCspHeaders(session.defaultSession, appOrigin, () => csp.current());
  const logLine = (line: string) => log.write(line);
  installPermissionPolicy(session.defaultSession as unknown as PermissionSession, {
    appOrigin,
    log: logLine,
  });
  installDenyAllPermissions(
    session.fromPartition(PREVIEW_PARTITION) as unknown as PermissionSession,
  );

  const activation = new UserActivationTracker();
  app.on("web-contents-created", (_event, contents: WebContents) => {
    hardenWebContents(contents as unknown as GuardedContents, {
      appOrigin,
      guestPreloadPath: null,
      log: logLine,
    });
    const id = contents.id;
    contents.on("input-event", (_e, input) => activation.noteInput(id, input.type));
    contents.once("destroyed", () => activation.forget(id));
  });

  const registry = new IpcRegistry({
    ipc: ipcMain as unknown as IpcMainLike,
    policy: { appOrigin, isKnownWebContents: (id) => windowsRef?.isKnownWebContents(id) ?? false },
    log: logLine,
  });
  const windows = new WindowManager({
    createWindow: (options) =>
      new BrowserWindow(options as BrowserWindowConstructorOptions) as unknown as BrowserWindowLike,
    screen,
    appOrigin,
    preloadPath: path.join(HERE, "preload.cjs"),
    dev,
    platform: process.platform,
    title: APP_NAME,
    windowStatePath: paths.windowState,
    zoomStatePath: paths.zoomState,
    timers: {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    },
    now: Date.now,
    log: logLine,
    prepareContents: () => {
      // web-contents-created already hardened it and tracks its input.
    },
    emitWindowState: (contents, state) => registry.emit(contents, "indus:window:state", state),
    emitZoom: (contents, state) => registry.emit(contents, "indus:zoom:changed", state),
  });
  windowsRef = windows;

  const probe = await probeKeychain({
    platform: process.platform,
    safeStorage: safeStorage as SafeStorageLike,
  });
  log.write(`[secrets] keychain ${probe.usable ? "usable" : `not usable (${probe.reason})`}`);
  const secrets = new DesktopSecrets({
    dir: paths.secretsDir,
    policyPath: paths.secretsPolicy,
    safeStorage: safeStorage as SafeStorageLike,
    probe,
    log: logLine,
  });

  const context: MainContext = {
    registry,
    windows,
    log,
    paths,
    appOrigin,
    csp,
    dev,
    packaged: app.isPackaged,
    platform: process.platform,
    appVersion: app.getVersion(),
    deepLinkScheme: instance.scheme,
    machinery: instance.machinery,
    activation,
    externalOpener: new ExternalOpener({
      openExternal: (url) => shell.openExternal(url),
      activation,
      log: logLine,
    }),
    secrets,
    shellPath,
  };

  // INSTALL STEPS — one line per task, in this order.
  installWindowIpc(context); // P20-05, P20-06
  installExternalOpenIpc({ registry, opener: context.externalOpener, isGuestOf: () => false }); // P20-02
  installSecretsIpc(registry, secrets); // P20-07
  registry.seal();

  Menu.setApplicationMenu(
    Menu.buildFromTemplate(
      appMenuTemplate({
        appName: APP_NAME,
        platform: process.platform,
        dev,
        zoom: (direction) => windows.stepZoomBy(direction),
        newWindow: () => windows.openInstance(),
      }) as MenuItemConstructorOptions[],
    ),
  );

  windows.openMain();
  app.on("activate", () => {
    const main = windows.mainWindow();
    if (main === null) windows.openMain();
    else windows.raise(main, "app-activate");
  });
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", () => {
    windows.saveNow();
    log.close();
  });
  if (latestDeepLink !== null) log.write("[deep-link] a link is waiting for the renderer (P20-34)");
}
