// Renderer entry (P20-15; profiles P20-26, sidebar P20-25, panes P20-27). The window kind and its
// options come from the query string; the route from the hash. One connection store per window;
// the profile switcher keeps one workspace (connection, transcripts, chat slots) per profile and
// the chat shows the foreground one. The first profile is the window's own (`?profile=`), else
// the one main's launch decision picked (`profile.get().launch`). A main window starts its
// foreground connection at once: `connection.get` starts (or joins) main's boot.

import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Root } from "./app/App.tsx";
import { desktopBridge, hasShell, launchFacts, readWindowQuery } from "./app/bridge.ts";
import { chatSlots } from "./app/chatSlots.tsx";
import { installErrorForwarding } from "./app/errorForwarding.ts";
import { mountFindBar } from "./app/FindBar.tsx";
import { settingsSlots } from "./app/settingsSlots.tsx";
import { applyStoredTheme } from "./app/theme.tsx";
import { ConnectionStore } from "./gateway/connectionStore.ts";
import { translate } from "./i18n/index.tsx";
import { registerWorkingPanes } from "./panes/d2Panes.tsx";
import { LayoutStore } from "./panes/layoutStore.ts";
import { paneRegistry } from "./panes/PaneShell.tsx";
import { profileKey } from "./profiles/storageFamilies.ts";
import { ProfileSwitcher } from "./profiles/switching.ts";
import { ChatWorkspace } from "./profiles/workspace.ts";
import { SidebarModel } from "./sidebar/Sidebar.tsx";

applyStoredTheme();
document.title = translate("app.name");

const query = readWindowQuery(window.location.search);
// Uncaught errors and rejections reach desktop.log through main (P20-39).
installErrorForwarding(window, (report) => desktopBridge()?.logs.rendererError(report), {
  source: `renderer:${query.kind}`,
});
const bridge = desktopBridge();

async function firstProfile(): Promise<string> {
  const own = query.profile ?? launchFacts().profile;
  if (own !== null) return profileKey(own);
  if (bridge === null || !(hasShell(query.kind) || query.kind === "hud")) return "default";
  try {
    return profileKey((await bridge.profile.get()).launch);
  } catch {
    return "default"; // main has no answer yet: the backend's default profile
  }
}

async function start(): Promise<void> {
  const initial = await firstProfile();
  const connections = new ConnectionStore({ defaultProfile: initial });
  const switcher = new ProfileSwitcher<ChatWorkspace>({
    initial,
    createWorkspace: (profile) =>
      new ChatWorkspace(profile, connections, (workspace) => ({
        ...chatSlots({
          connection: workspace.connection,
          transcripts: workspace.transcripts,
          profile: workspace.profile,
        }),
        ...settingsSlots({ connection: workspace.connection }),
      })),
    bridge: bridge?.profile ?? null,
    log: (line) => bridge?.logs.rendererError({ message: line, source: "renderer:profiles" }),
  });
  // `primary()` (chat view, status) follows the foreground profile
  switcher.subscribe(() => connections.setPrimary(switcher.getState().foreground));
  connections.setPrimary(initial);
  bridge?.connection.onChanged((descriptor) => switcher.noteDescriptor(descriptor));

  // the working panes (P20-28..31) join the pane registry before the layout reads it
  registerWorkingPanes();
  const sidebar = new SidebarModel();
  switcher.registerWipe((profile) => sidebar.wipe(profile));
  // only the main window keeps its arrangement: a popped-out or peer window must not overwrite it
  const layout = new LayoutStore({
    available: () => paneRegistry.ids(),
    ...(query.kind === "main" ? {} : { storage: null }),
  });

  // The HUD streams a conversation too (P20-35); the quick window has no connection of its own.
  if (hasShell(query.kind) || query.kind === "hud") switcher.start();

  // The network came back or the window woke: check the link instead of trusting it.
  const nudge = () => void switcher.foreground().connection.probeLiveness();
  window.addEventListener("online", nudge);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") nudge();
  });

  const element = document.getElementById("root");
  if (element === null) return;
  createRoot(element).render(
    <StrictMode>
      <Root
        connections={connections}
        window={query}
        switcher={switcher}
        chrome={{ sidebar, layout }}
      />
    </StrictMode>,
  );
  // Main reveals the window once the first frame is painted (no white flash).
  requestAnimationFrame(() => bridge?.windows.painted());
  if (hasShell(query.kind)) mountFindBar(); // find in the transcript (P20-39)
}

void start();
