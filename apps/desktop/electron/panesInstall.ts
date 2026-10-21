// Main's side of the working panes (P20-28 preview, P20-29 files, P20-30 git review, P20-31
// terminal), wired into the IPC registry. `main.ts` calls `installPanes(context, electron)` once in
// its install steps and gives the preview guard to the external opener and the webview attach hook.

import os from "node:os";
import path from "node:path";
import type { MainContext } from "./context.ts";
import { type DialogRequest, FsLimits, installFsIpc, type OwnerContents } from "./fs/fsIpc.ts";
import { PathGuard } from "./fs/pathGuard.ts";
import { findOnPath, GitOps, installGitIpc, resolveGitBinary } from "./git/gitOps.ts";
import type { EventTarget } from "./ipc/registry.ts";
import { WatchHub } from "./preview/fileWatch.ts";
import { type HostWindow, installPreviewIpc, PreviewGuests } from "./preview/guest.ts";
import {
  chooseShell,
  installTerminalIpc,
  isExecutableFile,
  loadNodePty,
  PtyHost,
  type TerminalOwner,
} from "./terminal/ptyHost.ts";

/** The Electron pieces the panes need (passed in so this file stays testable without Electron). */
export interface PaneElectron {
  /** `webContents.fromId(id)` limited to the windows the manager created. */
  contents: (id: number) => (OwnerContents & TerminalOwner) | null;
  /** The BrowserWindow hosting the web contents `id`. */
  hostWindow: (contentsId: number) => HostWindow | null;
  trashItem: (file: string) => Promise<void>;
  showItemInFolder: (file: string) => void;
  showDialog: (
    ownerId: number,
    request: DialogRequest,
  ) => Promise<{ canceled: boolean; paths: string[] }>;
  /** Run `fn` once when the app quits. */
  onQuit: (fn: () => void) => void;
  resourcesPath: string;
  homeDir?: string;
  /** Workspace roots a window declared and the guard accepted (main lets media serve them). */
  onWorkspaceRoots?: (roots: readonly string[]) => void;
}

export interface PanesHandle {
  guard: PathGuard;
  guests: PreviewGuests;
  watches: WatchHub;
  terminals: PtyHost;
}

export function installPanes(context: MainContext, electron: PaneElectron): PanesHandle {
  const { registry, log, platform } = context;
  const line = (text: string) => log.write(text);
  const guard = new PathGuard({ home: electron.homeDir ?? os.homedir(), platform });
  const watches = new WatchHub({ log: line });

  installFsIpc({
    registry,
    guard,
    watches,
    limits: new FsLimits(path.join(context.paths.userData, "fs-limits.json")),
    contents: electron.contents,
    trashItem: electron.trashItem,
    showItemInFolder: electron.showItemInFolder,
    showDialog: electron.showDialog,
    log: line,
    ...(electron.onWorkspaceRoots === undefined
      ? {}
      : { onRootsAccepted: electron.onWorkspaceRoots }),
  });

  const guests = new PreviewGuests({
    hostWindow: electron.hostWindow,
    hostContents: (id) => electron.contents(id) as EventTarget | null,
    registry,
    log: line,
  });
  installPreviewIpc({ registry, guests, guard, watches, contents: electron.contents });

  installGitIpc({
    registry,
    guard,
    ops: new GitOps({
      git: resolveGitBinary({
        platform,
        packaged: context.packaged,
        resourcesPath: electron.resourcesPath,
      }),
      findGh: () => findOnPath("gh", process.env, platform),
      env: process.env,
    }),
    log: line,
  });

  const terminals = new PtyHost({
    registry,
    guard,
    loadSpawn: async () => {
      await context.shellPath; // the login-shell PATH first (R08.16)
      return loadNodePty();
    },
    shell: () =>
      chooseShell({
        platform,
        env: process.env,
        isExecutable: isExecutableFile,
        findOnPath: (name) => findOnPath(name, process.env, platform),
      }),
    env: process.env,
    platform,
    appVersion: context.appVersion,
    contents: electron.contents,
    log: line,
  });
  installTerminalIpc(registry, terminals);

  electron.onQuit(() => {
    terminals.disposeAll();
    watches.closeAll();
  });

  return { guard, guests, watches, terminals };
}
