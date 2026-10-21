// The app windows' preload (P20-05). Bundled into one plain-JS CommonJS file by
// scripts/build-electron.mjs: a sandboxed preload can neither run TypeScript nor require anything
// but `electron`. It exposes `window.indusDesktop` (built from the channel table) and nothing
// else — no `ipcRenderer`, no Node.

import { contextBridge, ipcRenderer } from "electron";
import { buildBridge } from "./ipc/bridge.ts";

contextBridge.exposeInMainWorld("indusDesktop", buildBridge(ipcRenderer));
