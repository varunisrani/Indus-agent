// Clipboard text through main (P20-39): the renderer's own clipboard permission is denied by the
// permission policy, so copy and paste buttons go through the bridge; a plain browser preview (no
// bridge) falls back to `navigator.clipboard`.

import { desktopBridge } from "./bridge.ts";

export async function copyText(text: string): Promise<boolean> {
  const bridge = desktopBridge();
  try {
    if (bridge !== null) {
      await bridge.clipboard.writeText({ text });
      return true;
    }
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export async function readText(): Promise<string | null> {
  const bridge = desktopBridge();
  try {
    if (bridge !== null) return (await bridge.clipboard.readText()).text;
    return await navigator.clipboard.readText();
  } catch {
    return null;
  }
}
