// Applies the desktop preferences that change how this window looks (P20-32): the chat text
// size. Renders nothing.

import { useEffect } from "react";
import { desktopBridge } from "../app/bridge.ts";
import { type SettingsBridge, usePrefs } from "./context.tsx";

export const CHAT_TEXT_VAR = "--indus-chat-text-scale";

export function PrefsEffects(props: { bridge?: SettingsBridge | null }) {
  const bridge = props.bridge === undefined ? (desktopBridge() ?? null) : props.bridge;
  const { prefs } = usePrefs(bridge);
  const scale = prefs?.chatTextScale;
  useEffect(() => {
    if (scale === undefined) return;
    document.documentElement.style.setProperty(CHAT_TEXT_VAR, String(scale / 100));
  }, [scale]);
  return null;
}
