// The preview guest's preload (P20-28), bundled to `dist-electron/preview-guest.cjs` and attached
// by main only to webviews on the preview partition (`security/navigation.ts`). It exposes nothing
// to the page: no `contextBridge`, no globals. See `guestPreload.ts`.

import { ipcRenderer } from "electron";
import { installLinkHandOff } from "./guestPreload.ts";

installLinkHandOff(document, (channel, url) => ipcRenderer.sendToHost(channel, url));
