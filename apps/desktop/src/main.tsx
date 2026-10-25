// Renderer entry (P20-15; profiles P20-26, sidebar P20-25, panes P20-27). The window kind and its
// options come from the query string; the route from the hash. One connection store per window;
// the profile switcher keeps one workspace (connection, transcripts, chat slots) per profile and
// the chat shows the foreground one. The first profile is the window's own (`?profile=`), else
// the one main's launch decision picked (`profile.get().launch`). A main window starts its
// foreground connection at once: `connection.get` starts (or joins) main's boot.

import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/abril-fatface/400.css";
import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Root } from "./app/App.tsx";
import { desktopBridge, hasShell, launchFacts, readWindowQuery } from "./app/bridge.ts";
import { chatSlots } from "./app/chatSlots.tsx";
import { installErrorForwarding } from "./app/errorForwarding.ts";
import { mountFindBar } from "./app/FindBar.tsx";
import { managementSlots, rpcOf } from "./app/manage/managementSlots.tsx";
import { settingsSlots } from "./app/settingsSlots.tsx";
import { applyStoredTheme } from "./app/theme.tsx";
import { BotsPage, BotsSurface, botsContribution, registryHost } from "./bots/BotsPage.tsx";
import { mirrorThemeContributions } from "./contrib/areas.tsx";
import { startDesktopPlugins } from "./contrib/host.ts";
import { PluginsPage } from "./contrib/PluginsPage.tsx";
import { contributions } from "./contrib/registry.ts";
import { mirrorPaneContributions } from "./contrib/shell.tsx";
import { ConnectionStore } from "./gateway/connectionStore.ts";
import { translate } from "./i18n/index.tsx";
import { registerWorkingPanes } from "./panes/d2Panes.tsx";
import { LayoutStore } from "./panes/layoutStore.ts";
import { paneRegistry } from "./panes/PaneShell.tsx";
import type { LooseRpc } from "./pet/PetOverlay.tsx";
import { profileKey } from "./profiles/storageFamilies.ts";
import { ProfileSwitcher } from "./profiles/switching.ts";
import { ChatWorkspace } from "./profiles/workspace.ts";
import { SidebarModel } from "./sidebar/Sidebar.tsx";
import { armWhenReady, installAppWake } from "./wake/appWake.ts";

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
      new ChatWorkspace(profile, connections, (workspace) => {
        const settings = settingsSlots({ connection: workspace.connection });
        const manage = managementSlots({
          connection: workspace.connection,
          profile: workspace.profile,
        });
        return {
          ...chatSlots({
            connection: workspace.connection,
            transcripts: workspace.transcripts,
            profile: workspace.profile,
          }),
          ...settings,
          pages: manage.pages, // P24-27, P24-28 (Bots, P24-31, is a contributed route)
          overlays: { ...manage.overlays, ...settings.overlays },
        };
      }),
    bridge: bridge?.profile ?? null,
    log: (line) => bridge?.logs.rendererError({ message: line, source: "renderer:profiles" }),
  });
  // `primary()` (chat view, status) follows the foreground profile
  switcher.subscribe(() => connections.setPrimary(switcher.getState().foreground));
  connections.setPrimary(initial);
  bridge?.connection.onChanged((descriptor) => switcher.noteDescriptor(descriptor));

  // the working panes (P20-28..31) join the pane registry before the layout reads it; contributed
  // panes (P24-32) follow the registry's `pane` area from then on
  registerWorkingPanes();
  if (hasShell(query.kind)) {
    mirrorPaneContributions();
    // contributed themes join Settings › Appearance (P24-32)
    mirrorThemeContributions();
    // the plugin inventory as a tool pane too (the layout's Open list), beside Settings › Plugins
    contributions.register({
      id: "app.plugins",
      area: "pane",
      source: "app",
      title: translate("plugins.pane"),
      render: () => <PluginsPage />,
    });
  }
  // The Bots surface (P24-31) is a bundled contribution in the app's registry (P24-32): its route
  // and its sidebar entry, both read by the shell. Registered before the first render so a link
  // to /bots opens it.
  if (hasShell(query.kind)) {
    new BotsSurface(
      registryHost(),
      botsContribution(
        () => <BotsPage rpc={rpcOf(switcher.foreground().connection)} />,
        translate("nav.bots"),
      ),
    ).setEnabled(true);
  }
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
  // The wake word (P24-20) over the foreground connection; a detection dictates into the
  // composer (P24-35). Armed at launch only when it was turned on (consent stays with the switch).
  if (bridge !== null && query.kind === "main") {
    const { controller } = installAppWake(
      () => switcher.foreground().connection as unknown as LooseRpc,
    );
    armWhenReady(controller, switcher.foreground().connection); // W54-D2-3
  }
  // Desktop plugins (P24-32): checked in main, loaded here through a host built from the bridge.
  if (bridge !== null && hasShell(query.kind)) {
    void startDesktopPlugins({
      bridge,
      connection: () => switcher.foreground().connection,
      profile: () => switcher.getState().foreground,
    });
  }
  // Main reveals the window once the first frame is painted (no white flash).
  requestAnimationFrame(() => bridge?.windows.painted());
  if (hasShell(query.kind)) mountFindBar(); // find in the transcript (P20-39)
}

void start();
