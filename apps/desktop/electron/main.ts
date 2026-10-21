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

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  app,
  BrowserWindow,
  type BrowserWindowConstructorOptions,
  clipboard,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  type MenuItemConstructorOptions,
  Notification,
  nativeImage,
  net,
  powerMonitor,
  powerSaveBlocker,
  protocol,
  safeStorage,
  screen,
  session,
  shell,
  Tray,
  type WebContents,
  webContents,
} from "electron";
import { installAmbient } from "./ambient/install.ts";
import { LINK_PARTITION, type LinkWindowLike } from "./ambient/linkPreview.ts";
import type { ElectronNotificationLike } from "./ambient/notifications.ts";
import type { TrayLike } from "./ambient/tray.ts";
import { trayBitmap } from "./ambient/trayIcon.ts";
import { appMenuTemplate } from "./appMenu.ts";
import { type AppOrigin, devServerOrigin, fileAppOrigin } from "./appOrigin.ts";
import { installArtifactsIpc } from "./artifacts/download.ts";
import { type ElectronHooks, installBackend } from "./backend/install.ts";
import { runCommand } from "./backend/process.ts";
import { resolveBackend } from "./backend/resolver.ts";
import type { MainContext } from "./context.ts";
import { installContextMenus, type MenuContentsLike } from "./contextMenu.ts";
import { DeepLinkRouter, installDeepLinks } from "./deepLinks.ts";
import { installDiagnostics } from "./diagnostics.ts";
import { applyEnvDefaults, parseBakedEnvDefaults } from "./envDefaults.ts";
import { type IpcMainLike, IpcRegistry } from "./ipc/registry.ts";
import { installJournalIpc, PreparedSubmissions } from "./journal/preparedSubmissions.ts";
import { chooseOzonePlatform, planSwitches, readLaunchConfig } from "./launchConfig.ts";
import { installLinuxLauncher } from "./linuxLauncher.ts";
import { DesktopLog } from "./log.ts";
import { installMediaProtocol, MediaRoots, type MediaTarget } from "./mediaProtocol.ts";
import { installPanes, type PaneElectron, type PanesHandle } from "./panesInstall.ts";
import { desktopPaths, resolveIndusHome, userDataOverride } from "./paths.ts";
import { installDiskPlugins } from "./plugins/diskPlugins.ts";
import { installPrefsIpc } from "./prefs.ts";
import { type BackendPort, installProfiles } from "./profiles/routing.ts";
import {
  DesktopSecrets,
  installSecretsIpc,
  probeKeychain,
  type SafeStorageLike,
} from "./secrets.ts";
import {
  CspState,
  installCspHeaders,
  MEDIA_SCHEME,
  PLUGIN_SCHEME,
  PLUGIN_SCHEME_PRIVILEGES,
} from "./security/csp.ts";
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
import {
  acquireSingleInstance,
  deepLinkScheme,
  extractDeepLink,
  type LockApp,
  lockTempDir,
} from "./singleInstance.ts";
import { type Fetcher, installThemeMarketplace } from "./theme/marketplace.ts";
import { installUninstall } from "./uninstall.ts";
import { createUpdateGate, gateHooks, installUpdates } from "./updates/install.ts";
import { installWindowIpc } from "./windows/ipc.ts";
import { type BrowserWindowLike, WindowManager } from "./windows/manager.ts";
import { installPetWindow } from "./windows/petWindow.ts";
import { installWakeIndicator } from "./windows/wakeIndicator.ts";

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
  // P24-32: checked desktop plugin modules (ES module imports need a standard, CORS-enabled scheme)
  { scheme: PLUGIN_SCHEME, privileges: { ...PLUGIN_SCHEME_PRIVILEGES } },
]);

// Every deep link — cold-start argv, a second launch, macOS open-url — goes into one queue that
// main flushes once the main window's page listens (P20-34, R08.21).
const deepLinks = new DeepLinkRouter(deepLinkScheme(dev));
let windowsRef: WindowManager | null = null;
let panesRef: PanesHandle | null = null;

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
    if (launch.deepLink !== null) {
      // The link is delivered without taking focus (P20-34); the router opens a window if none.
      deepLinks.receive(launch.deepLink);
      return;
    }
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
    deepLinks.receive(url); // P20-34
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

/** The Electron pieces the ambient step uses (P20-35, P20-36). */
function ambientElectron(): Parameters<typeof installAmbient>[1] {
  const linkSession = session.fromPartition(LINK_PARTITION);
  installDenyAllPermissions(linkSession as unknown as PermissionSession);
  linkSession.on("will-download", (_event, item) => item.cancel());
  return {
    app,
    globalShortcut,
    screen,
    powerSaveBlocker,
    powerMonitor,
    createTray: () => {
      const bitmap = trayBitmap(process.platform);
      const image = nativeImage.createFromBitmap(Buffer.from(bitmap.pixels), {
        width: bitmap.width,
        height: bitmap.height,
      });
      if (bitmap.template) image.setTemplateImage(true);
      return new Tray(image) as unknown as TrayLike;
    },
    buildMenu: (items) => Menu.buildFromTemplate(items as MenuItemConstructorOptions[]),
    notification: {
      isSupported: () => Notification.isSupported(),
      create: (options) => new Notification(options) as unknown as ElectronNotificationLike,
    },
    createLinkWindow: (options) =>
      new BrowserWindow(options as BrowserWindowConstructorOptions) as unknown as LinkWindowLike,
    fetch: (url, init) => linkSession.fetch(url, init),
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
  const previewSession = session.fromPartition(PREVIEW_PARTITION);
  installDenyAllPermissions(previewSession as unknown as PermissionSession);

  const activation = new UserActivationTracker();
  app.on("web-contents-created", (_event, contents: WebContents) => {
    hardenWebContents(contents as unknown as GuardedContents, {
      appOrigin,
      guestPreloadPath: path.join(HERE, "preview-guest.cjs"), // P20-28
      log: logLine,
    });
    // A preview guest attached (only the preview partition gets this far): track it for the
    // external opener, capture and the fullscreen key hook (P20-28).
    contents.on("did-attach-webview", (_event, guest) => {
      panesRef?.guests.adopt(
        guest as unknown as Parameters<PanesHandle["guests"]["adopt"]>[0],
        guest.session === previewSession ? PREVIEW_PARTITION : "",
      );
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
  installExternalOpenIpc({
    registry,
    opener: context.externalOpener,
    isGuestOf: (guest, host) => panesRef?.guests.isGuestOf(guest, host) ?? false, // P20-28
  }); // P20-02
  installSecretsIpc(registry, secrets); // P20-07
  installJournalIpc(
    registry,
    new PreparedSubmissions({
      dir: path.join(paths.userData, "prepared-submissions"),
      log: logLine,
    }),
  ); // P20-20
  let backendPort: BackendPort | null = null;
  const profiles = installProfiles(context, {
    argv: process.argv.slice(1),
    backend: () => backendPort,
  }); // P20-26
  const updateGate = createUpdateGate(context); // P20-37 (the boot waits on it)
  const backend = installBackend(
    context,
    {
      app: app as unknown as ElectronHooks["app"],
      webRequest: session.defaultSession.webRequest as unknown as ElectronHooks["webRequest"],
      powerMonitor,
      showMessageBox: (options) => dialog.showMessageBox(options),
      argv: process.argv.slice(1),
      execPath: process.execPath,
      homeDir: os.homedir(),
      isWsl:
        process.platform === "linux" &&
        (process.env.WSL_DISTRO_NAME !== undefined || /microsoft/i.test(os.release())),
      updateGate: gateHooks(updateGate),
    },
    { launchProfile: () => profiles.launch().profile },
  ); // P20-08..14
  installArtifactsIpc(context.registry, {
    current: () => backend.orchestrator.current(),
    downloadsDir: () => app.getPath("downloads"),
    log: logLine,
  }); // P24-28
  backendPort = {
    attachedProfile: () => {
      const descriptor = backend.orchestrator.current()?.descriptor;
      return descriptor === undefined ? null : (descriptor.profile ?? "default");
    },
    rehome: async (profile) => {
      await backend.orchestrator.start(profile);
    },
  }; // P20-26
  const diagnostics = installDiagnostics(context, {
    clipboard,
    revealPath: (file) => shell.showItemInFolder(file),
    facts: { arch: process.arch, versions: process.versions, locale: app.getLocale() },
    homeDir: os.homedir(),
    env: process.env,
    resourcesPath: app.isPackaged ? process.resourcesPath : null,
    // the serve session token or the remote token of the current connection, never in a bundle
    knownSecrets: () => {
      const token = backend.orchestrator.current()?.rest.token;
      return typeof token === "string" && token !== "" ? [token] : [];
    },
  }); // P20-39
  const prefs = installPrefsIpc(context); // P20-32
  installAmbient(context, ambientElectron(), backend, { prefs }); // P20-35, P20-36 (P20-32 prefs)
  installUpdates(
    context,
    {
      app,
      fetch: (url, init) => net.fetch(url as string, init),
      runtimeVersion: () => backend.orchestrator.current()?.descriptor.codeVersion ?? null,
    },
    {
      gate: updateGate,
      stopAppWork: async () => {
        if (backend.installer.running()) backend.installer.cancel();
      },
    },
  ); // P20-37
  const mediaRoots = new MediaRoots([
    os.homedir(),
    path.join(paths.home, "media"),
    path.join(paths.home, "cache"),
  ]);
  installMediaProtocol(protocol, {
    roots: mediaRoots,
    target: (): MediaTarget | null => {
      const connection = backend.orchestrator.current();
      if (connection === null) return null;
      return connection.descriptor.mode === "remote"
        ? {
            mode: "remote",
            origin: connection.rest.origin,
            token: connection.rest.token,
            profile: connection.descriptor.profile,
          }
        : { mode: "local" };
    },
    log: logLine,
  }); // P20-34
  installDiskPlugins(context, {
    protocol,
    fs: {
      readdir: (dir) => fs.promises.readdir(dir),
      stat: (file) => fs.promises.stat(file),
      readFile: (file) => fs.promises.readFile(file),
    },
    watch: (dir, onEvent) => {
      try {
        return fs.watch(dir, { persistent: false }, () => onEvent());
      } catch {
        return null; // the folder does not exist yet: no hot reload until the next start
      }
    },
  }); // P24-32
  installUninstall(context, {
    home: paths.home,
    platform: process.platform,
    pid: process.pid,
    execPath: process.execPath,
    indus: async () => {
      const found = await resolveBackend({
        env: process.env,
        argv: process.argv.slice(1),
        platform: process.platform,
        packaged: app.isPackaged,
        home: paths.home,
        homeDir: os.homedir(),
        devCheckoutRoot: app.isPackaged ? null : path.resolve(app.getAppPath(), "..", ".."),
        appExecPath: process.execPath,
        isWsl: process.platform === "linux" && process.env.WSL_DISTRO_NAME !== undefined,
        log: logLine,
      });
      return found.kind === "found" ? found.backend : null;
    },
    run: async (indus, args, timeoutMs) => {
      const result = await runCommand(indus, args, { timeoutMs });
      return result.code === 0 ? result.stdout : null;
    },
    readReceipts: async () => {
      const dir = path.join(paths.home, "logs", "install_receipts");
      const names = await fs.promises.readdir(dir).catch(() => [] as string[]);
      const found: unknown[] = [];
      for (const name of names.filter((n) => n.endsWith(".json"))) {
        try {
          found.push(JSON.parse(await fs.promises.readFile(path.join(dir, name), "utf8")));
        } catch {
          // an unreadable receipt confirms nothing
        }
      }
      return found;
    },
    writeHelper: async (text, extension) => {
      const file = path.join(
        os.tmpdir(),
        `indus-uninstall-${randomBytes(6).toString("hex")}${extension}`,
      );
      await fs.promises.writeFile(file, text, { encoding: "utf8", mode: 0o700, flag: "wx" });
      return file;
    },
    spawnDetached: context.machinery.guard(
      "starting the uninstall helper",
      (command: string, args: readonly string[]) => {
        spawn(command, [...args], { detached: true, stdio: "ignore", windowsHide: true }).unref();
      },
    ),
    quit: () => app.quit(),
    log: logLine,
  }); // P24-34
  const startDeepLinks = installDeepLinks(context, deepLinks); // P20-34
  panesRef = installPanes(context, {
    ...paneElectron(windows),
    onWorkspaceRoots: (roots) => {
      for (const root of roots) mediaRoots.add(root);
    },
  }); // P20-28..31
  installContextMenus({
    dev,
    platform: process.platform,
    onWebContentsCreated: (listener) =>
      app.on("web-contents-created", (_event, contents) =>
        listener(contents as unknown as MenuContentsLike),
      ),
    popup: (template, contents) => {
      const owner = BrowserWindow.fromWebContents(contents as unknown as WebContents);
      Menu.buildFromTemplate(template as MenuItemConstructorOptions[]).popup(
        owner === null ? {} : { window: owner },
      );
    },
    writeText: (text) => clipboard.writeText(text),
    isAppWindow: (id) => windows.isKnownWebContents(id),
    emitFindOpen: (contents) =>
      registry.emit(contents as unknown as WebContents, "indus:find:open", {}),
  }); // P20-39
  installPetWindow(context, { screen }); // P24-19
  installWakeIndicator(context, { screen }); // P24-20
  installThemeMarketplace(context, {
    fetch: ((url: string, init: RequestInit) => net.fetch(url, init)) as unknown as Fetcher,
  }); // P24-33
  registry.seal();

  Menu.setApplicationMenu(
    Menu.buildFromTemplate(
      appMenuTemplate({
        appName: APP_NAME,
        platform: process.platform,
        dev,
        zoom: (direction) => windows.stepZoomBy(direction),
        newWindow: () => windows.openInstance(),
        help: diagnostics.menuItems, // P20-39
      }) as MenuItemConstructorOptions[],
    ),
  );

  installLinuxLauncher(windows.openMain(), {
    platform: process.platform,
    packaged: app.isPackaged,
    env: process.env,
    homeDir: os.homedir(),
    resourcesPath: process.resourcesPath,
    scheme: instance.scheme,
    log: logLine,
  }); // P20-38
  startDeepLinks();
  const coldStartLink = extractDeepLink(process.argv.slice(1), instance.scheme);
  if (coldStartLink !== null) deepLinks.receive(coldStartLink);
  app.on("activate", () => {
    const main = windows.mainWindow();
    if (main === null) windows.openMain();
    else windows.raise(main, "app-activate");
  });
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", (event) => {
    if (event.defaultPrevented) return; // the quit guard is asking first (P20-13)
    windows.saveNow();
    log.close();
  });
}

/** The Electron pieces the working panes use (P20-28..31). */
function paneElectron(windows: WindowManager): PaneElectron {
  const known = (id: number) => {
    const contents = webContents.fromId(id);
    return contents !== undefined && windows.isKnownWebContents(id) ? contents : null;
  };
  return {
    contents: (id) => known(id) as unknown as ReturnType<PaneElectron["contents"]>,
    hostWindow: (id) => {
      const contents = known(id);
      return contents === null ? null : BrowserWindow.fromWebContents(contents);
    },
    trashItem: (file) => shell.trashItem(file),
    showItemInFolder: (file) => shell.showItemInFolder(file),
    showDialog: async (ownerId, request) => {
      const contents = known(ownerId);
      const owner = contents === null ? null : BrowserWindow.fromWebContents(contents);
      const common = {
        ...(request.title === undefined ? {} : { title: request.title }),
        ...(request.defaultPath === undefined ? {} : { defaultPath: request.defaultPath }),
      };
      if (request.mode === "save") {
        const answer =
          owner === null
            ? await dialog.showSaveDialog(common)
            : await dialog.showSaveDialog(owner, common);
        const file = answer.filePath ?? "";
        return { canceled: answer.canceled || file === "", paths: file === "" ? [] : [file] };
      }
      const properties: Array<
        "openFile" | "openDirectory" | "createDirectory" | "multiSelections"
      > = request.mode === "open-dir" ? ["openDirectory", "createDirectory"] : ["openFile"];
      if (request.multiple) properties.push("multiSelections");
      const answer =
        owner === null
          ? await dialog.showOpenDialog({ ...common, properties })
          : await dialog.showOpenDialog(owner, { ...common, properties });
      return { canceled: answer.canceled, paths: answer.filePaths };
    },
    onQuit: (fn) => app.on("will-quit", fn),
    resourcesPath: process.resourcesPath,
  };
}
