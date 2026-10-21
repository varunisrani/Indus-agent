// The management pages and overlays a profile workspace mounts (P24-27, P24-28, P24-31): main.tsx
// spreads these into the workspace's slots next to the chat and settings slots.

import { useLocation } from "react-router";
import { BotsPage } from "../../bots/BotsPage.tsx";
import type { RpcLike } from "../../bots/canonicalChat.ts";
import { desktopPluginSource } from "../../contrib/host.ts";
import type { GatewayConnection } from "../../gateway/connectionStore.ts";
import { useConnectionState } from "../../gateway/react.tsx";
import type { AppSlots } from "../App.tsx";
import { AgentsOverlay, type SessionEventSource } from "../agents/AgentsOverlay.tsx";
import { ArtifactsPage } from "../artifacts/ArtifactsPage.tsx";
import { CapabilitiesRoute, type CapabilitySummary } from "../capabilities/CapabilitiesPage.tsx";
import { CommandCenter } from "../command-center/CommandCenter.tsx";
import { CronOverlay } from "../cron/CronOverlay.tsx";
import { LearningMap } from "../learning-map/LearningMap.tsx";
import { MessagingPage } from "../messaging/MessagingPage.tsx";
import { ProfilesOverlay } from "../profiles/ProfilesOverlay.tsx";
import { backgroundOf, classifyPath } from "../routes.ts";
import { WebhooksPage } from "../webhooks/WebhooksPage.tsx";

/** The daemon's `bots.*` / `groups.*` methods through the window's connection. Those names are
 * not in the generated protocol types yet (wiring for the integrator), hence the widening. */
export function rpcOf(connection: GatewayConnection): RpcLike {
  return {
    request: (method, params) =>
      (
        connection.request as unknown as (m: string, p: Record<string, unknown>) => Promise<unknown>
      )(method, params),
  };
}

function MessagingRoute(props: { connection: GatewayConnection; profile: string | null }) {
  const state = useConnectionState(props.connection);
  return (
    <MessagingPage profile={props.profile} instanceId={state.descriptor?.instanceId ?? null} />
  );
}

function AgentsRoute(props: { connection: GatewayConnection; onClose: () => void }) {
  const location = useLocation();
  const view = classifyPath(backgroundOf(location.state) ?? "/");
  return (
    <AgentsOverlay
      connection={props.connection as unknown as SessionEventSource}
      sessionId={view.kind === "chat" ? view.sessionId : null}
      onClose={props.onClose}
    />
  );
}

/** `capabilities.summary` of this workspace's profile; another profile picked on the page has no
 * connection here, so its pills show no count. */
function summaryOf(connection: GatewayConnection, own: string | null) {
  return async (profile: string): Promise<CapabilitySummary | null> => {
    if (own !== null && profile !== own) return null;
    try {
      return await connection.request("capabilities.summary", {});
    } catch {
      return null; // an older daemon: no counts
    }
  };
}

export function managementSlots(deps: { connection: GatewayConnection; profile: string | null }): {
  pages: NonNullable<AppSlots["pages"]>;
  overlays: NonNullable<AppSlots["overlays"]>;
} {
  const { connection, profile } = deps;
  return {
    pages: {
      messaging: () => <MessagingRoute connection={connection} profile={profile} />,
      webhooks: () => <WebhooksPage profile={profile} />,
      artifacts: () => <ArtifactsPage profile={profile} />,
      "command-center": () => <CommandCenter profile={profile} />,
      "learning-map": () => <LearningMap profile={profile} />,
      bots: () => <BotsPage rpc={rpcOf(connection)} />,
      // P24-26: the counts from the daemon and the Desktop column from the plugin loader (P24-32)
      capabilities: () => (
        <CapabilitiesRoute
          {...(profile === null ? {} : { profile })}
          summary={summaryOf(connection, profile)}
          desktopPlugins={desktopPluginSource}
        />
      ),
    },
    overlays: {
      cron: (onClose) => <CronOverlay profile={profile} onClose={onClose} />,
      profiles: (onClose) => <ProfilesOverlay onClose={onClose} />,
      agents: (onClose) => <AgentsRoute connection={connection} onClose={onClose} />,
    },
  };
}
