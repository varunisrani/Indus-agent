// Applies the desktop preferences that change how this window looks (P20-32): the chat text
// size, the rebindable key actions (W59-K1-2), and the display choices of Settings › Appearance (W55-F3) — published to the display
// store for the transcript, sidebar and shell, and mirrored on the root element for the rules in
// `displayPrefs.css`. Renders nothing but the cold-start "reopen last chat" step.

import { useEffect } from "react";
import { desktopBridge, launchFacts } from "../app/bridge.ts";
import { type SettingsBridge, usePrefs } from "./context.tsx";
import { publishDisplayPrefs } from "./displayPrefs.ts";
import { useKeyActionDispatch } from "./keyActions.ts";
import { ReopenLastChat } from "./ReopenLastChat.tsx";
import "./displayPrefs.css";

export const CHAT_TEXT_VAR = "--indus-chat-text-scale";
/** How much of the bubble colour a prompt bubble keeps (100% solid … 0% outline only). */
export const BUBBLE_FILL_VAR = "--indus-bubble-fill";

export function PrefsEffects(props: { bridge?: SettingsBridge | null }) {
  const bridge = props.bridge === undefined ? (desktopBridge() ?? null) : props.bridge;
  const { prefs } = usePrefs(bridge);
  // the rebindable composer, voice and profile keys (W59-K1-2), under the person's own bindings
  useKeyActionDispatch(prefs?.keybindings ?? null, launchFacts().platform);
  const scale = prefs?.chatTextScale;
  useEffect(() => {
    if (scale === undefined) return;
    document.documentElement.style.setProperty(CHAT_TEXT_VAR, String(scale / 100));
  }, [scale]);
  useEffect(() => {
    if (prefs !== null) publishDisplayPrefs(prefs);
  }, [prefs]);
  const clear = prefs?.bubbleTransparency;
  const tabStrip = prefs?.tabStrip;
  useEffect(() => {
    const root = document.documentElement;
    if (clear === undefined) root.style.removeProperty(BUBBLE_FILL_VAR);
    else root.style.setProperty(BUBBLE_FILL_VAR, `${100 - clear}%`);
    if (tabStrip === undefined) delete root.dataset.tabStrip;
    else root.dataset.tabStrip = tabStrip;
  }, [clear, tabStrip]);
  return <ReopenLastChat />;
}
