// The management pages and overlays a profile workspace mounts (P24-27, P24-28): main.tsx spreads
// these into the workspace's slots next to the chat and settings slots. The Bots page (P24-31) is
// not a slot: it is a bundled contribution's route (main.tsx, `contrib/shell.tsx`).

import { useLocation, useNavigate } from "react-router";
import type { RpcLike } from "../../bots/canonicalChat.ts";
import { desktopPluginSource } from "../../contrib/host.ts";
import type { GatewayConnection } from "../../gateway/connectionStore.ts";
import { useConnectionState } from "../../gateway/react.tsx";
import type { AppSlots } from "../App.tsx";
import { AgentsOverlay, type SessionEventSource } from "../agents/AgentsOverlay.tsx";
import { ArtifactsPage } from "../artifacts/ArtifactsPage.tsx";
import { CapabilitiesRoute, type CapabilitySummary } from "../capabilities/CapabilitiesPage.tsx";
import { CommandCenter, isSection } from "../command-center/CommandCenter.tsx";
import { CronOverlay } from "../cron/CronOverlay.tsx";
import { LearningMap } from "../learning-map/LearningMap.tsx";
import { MessagingPage } from "../messaging/MessagingPage.tsx";
import { ProfilesOverlay } from "../profiles/ProfilesOverlay.tsx";
import {
  backgroundOf,
  classifyPath,
  overlayPath,
  overlayStateFrom,
  sessionPath,
} from "../routes.ts";
import { SessionImport } from "../session-import/SessionImport.tsx";
import { WebhooksOverlay } from "../webhooks/WebhooksPage.tsx";

const RPC_OF = new WeakMap<GatewayConnection, RpcLike>();

/** The daemon's `bots.*` / `groups.*` methods and the connection's notifications (a room's
 * `groups.event`) through the window's connection. One object per connection, so a page's
 * effects keyed on it (a room's watch) do not restart on every render. The method names are
 * widened to plain strings here. */
export function rpcOf(connection: GatewayConnection): RpcLike {
  const known = RPC_OF.get(connection);
  if (known !== undefined) return known;
  const rpc: RpcLike = {
    request: (method, params) =>
      (
        connection.request as unknown as (m: string, p: Record<string, unknown>) => Promise<unknown>
      )(method, params),
    onEvent: (listener) =>
      connection.onEvent((event) =>
        listener(event as unknown as { type: string } & Record<string, unknown>),
      ),
  };
  RPC_OF.set(connection, rpc);
  return rpc;
}

function MessagingRoute(props: { connection: GatewayConnection; profile: string | null }) {
  const state = useConnectionState(props.connection);
  return (
    <MessagingPage profile={props.profile} instanceId={state.descriptor?.instanceId ?? null} />
  );
}

/** The command center keeps its section in the query (`?section=`), so a re-open lands there. */
function CommandCenterRoute(props: { profile: string | null; onClose: () => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  const raw = new URLSearchParams(location.search).get("section");
  return (
    <CommandCenter
      profile={props.profile}
      section={isSection(raw) ? raw : "sessions"}
      onSection={(section) =>
        navigate(
          { pathname: overlayPath("command-center"), search: `?section=${section}` },
          { replace: true, state: location.state },
        )
      }
      onClose={props.onClose}
      onOpenSession={(id) => navigate(sessionPath(id), { replace: true })}
      onOpenImport={() =>
        navigate(overlayPath("session-import"), {
          state: overlayStateFrom(location.pathname, location.state),
        })
      }
    />
  );
}

function SessionImportRoute(props: {
  connection: GatewayConnection;
  profile: string | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const state = useConnectionState(props.connection);
  const remote = state.descriptor?.mode === "remote" ? state.descriptor.wsUrl : null;
  let host: string | null = null;
  if (remote !== null) {
    try {
      host = new URL(remote).host;
    } catch {
      host = null;
    }
  }
  return (
    <SessionImport
      profile={props.profile}
      host={host}
      onClose={props.onClose}
      onImported={(id) => navigate(sessionPath(id), { replace: true })}
    />
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
      artifacts: () => <ArtifactsPage profile={profile} />,
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
      // W52-l-1: the management views are overlays
      webhooks: (onClose) => <WebhooksOverlay profile={profile} onClose={onClose} />,
      "command-center": (onClose) => <CommandCenterRoute profile={profile} onClose={onClose} />,
      "learning-map": (onClose) => <LearningMap profile={profile} onClose={onClose} />,
      "session-import": (onClose) => (
        <SessionImportRoute connection={connection} profile={profile} onClose={onClose} />
      ),
    },
  };
}
