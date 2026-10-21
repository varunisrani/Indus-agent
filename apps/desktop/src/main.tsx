// Renderer entry (P20-15). The window kind and its options come from the query string; the
// route from the hash. One connection store per window. A main window starts its primary
// connection at once: `connection.get` starts (or joins) main's boot and answers when the backend
// is ready; a boot failure leaves the store failed and the boot overlay's ready starts it again.

import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Root } from "./app/App.tsx";
import { desktopBridge, hasShell, launchFacts, readWindowQuery } from "./app/bridge.ts";
import { chatSlots } from "./app/chatSlots.tsx";
import { applyStoredTheme } from "./app/theme.tsx";
import { TranscriptStore } from "./chat/transcriptStore.ts";
import { ConnectionStore } from "./gateway/connectionStore.ts";
import { translate } from "./i18n/index.tsx";

applyStoredTheme();
document.title = translate("app.name");

const query = readWindowQuery(window.location.search);
const profile = query.profile ?? launchFacts().profile;
const connections = new ConnectionStore({ defaultProfile: profile });
const transcripts = new TranscriptStore(connections.primary());
const slots = chatSlots({ connection: connections.primary(), transcripts, profile });
if (hasShell(query.kind)) connections.primary().start();

// The network came back or the window woke: check the link instead of trusting it.
const nudge = () => void connections.primary().probeLiveness();
window.addEventListener("online", nudge);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") nudge();
});

const element = document.getElementById("root");
if (element !== null) {
  createRoot(element).render(
    <StrictMode>
      <Root connections={connections} transcripts={transcripts} window={query} slots={slots} />
    </StrictMode>,
  );
  // Main reveals the window once the first frame is painted (no white flash).
  requestAnimationFrame(() => desktopBridge()?.windows.painted());
}
