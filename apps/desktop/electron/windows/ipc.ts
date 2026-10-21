// IPC for windows, zoom and launch facts (P20-05, P20-06).

import os from "node:os";
import type { MainContext } from "../context.ts";
import { TITLE_BAR_HEIGHT } from "./factory.ts";

function safeHomeDir(): string {
  try {
    return os.homedir();
  } catch {
    return "";
  }
}

export function installWindowIpc(context: MainContext): void {
  const { registry, windows } = context;

  registry.sync("indus:facts", ({ senderId }) => {
    const described = windows.describe(senderId);
    return {
      platform: context.platform,
      appVersion: context.appVersion,
      packaged: context.packaged,
      dev: context.dev,
      windowKind: described?.kind ?? "main",
      watch: described?.watch ?? false,
      profile: described?.profile ?? null,
      homeDir: safeHomeDir(),
      titleBarHeight: TITLE_BAR_HEIGHT,
      deepLinkScheme: context.deepLinkScheme,
    };
  });

  registry.on("indus:window:painted", (_payload, { senderId }) => windows.painted(senderId));
  registry.on("indus:window:control", ({ action }, { senderId }) =>
    windows.control(senderId, action),
  );
  registry.on("indus:window:theme", ({ titleBar }, { senderId }) => {
    if (titleBar !== undefined) windows.setTitleBar(senderId, titleBar);
  });
  registry.handle("indus:window:open-session", ({ sessionId, watch, profile }) => {
    windows.openSecondary({
      sessionId,
      ...(watch === undefined ? {} : { watch }),
      ...(profile === undefined ? {} : { profile }),
    });
    return { opened: true };
  });
  registry.handle("indus:window:open-instance", ({ profile }) => {
    windows.openInstance(profile === undefined ? {} : { profile });
    return { opened: true };
  });

  registry.handle("indus:zoom:get", () => windows.zoomState());
  registry.handle("indus:zoom:set", ({ level }) => windows.setZoom(level));
  registry.handle("indus:zoom:step", ({ direction }) => windows.stepZoomBy(direction));
}
