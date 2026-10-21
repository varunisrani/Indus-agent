// The slots P20-32..34 fill: the Settings overlay route, and the window-wide layers — provider
// onboarding, the deep-link receiver and the desktop preferences that change this window's look.

import { useLocation, useNavigate } from "react-router";
import { DeepLinkHost } from "../deepLinks/DeepLinkHost.tsx";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import { useConnectionState } from "../gateway/react.tsx";
import { Onboarding } from "../onboarding/Onboarding.tsx";
import { requestOnboarding } from "../onboarding/requests.ts";
import { PrefsEffects } from "../settings/PrefsEffects.tsx";
import { SettingsOverlay } from "../settings/SettingsOverlay.tsx";
import type { AppSlots } from "./App.tsx";
import { overlayPath } from "./routes.ts";

function SettingsRoute(props: { onClose: () => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <SettingsOverlay
      query={location.search}
      onClose={props.onClose}
      // Settings (a dialog in its own layer) closes first, so the onboarding flow is not under it
      openOnboarding={() => {
        props.onClose();
        requestOnboarding({ reason: "manual" });
      }}
      onNavigate={(query) =>
        navigate(
          { pathname: overlayPath("settings"), search: `?${query}` },
          { replace: true, state: location.state },
        )
      }
    />
  );
}

function OnboardingLayer(props: { connection: GatewayConnection }) {
  const state = useConnectionState(props.connection);
  const connection = props.connection;
  return (
    <Onboarding
      ready={state.phase === "open"}
      events={(listener) => connection.onEvent(listener)}
      saveKey={async (slug, apiKey) => {
        await connection.request("model.save_key", { slug, api_key: apiKey });
      }}
    />
  );
}

export function settingsSlots(deps: {
  connection: GatewayConnection;
}): Pick<AppSlots, "overlays" | "layers"> {
  return {
    overlays: { settings: (onClose) => <SettingsRoute onClose={onClose} /> },
    layers: (
      <>
        <PrefsEffects />
        <OnboardingLayer connection={deps.connection} />
        <DeepLinkHost />
      </>
    ),
  };
}
