// The real screen for the admin console: xterm.js with the fit and Unicode 11 addons (the chat's
// packages; DOM renderer only, the console is small). Colours follow the active theme: black with
// warm text on dark, a pale canvas with dark text on light.

import { ansi } from "@indus/ui";
import type { ConsoleTerminal } from "./consoleSession.ts";

export type ConsoleTerminalFactory = () => Promise<ConsoleTerminal>;

function dark(): boolean {
  const set = document.documentElement.dataset.theme;
  if (set === "indus-dark") return true;
  if (set === "indus-light") return false;
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

/** xterm colours for the console in the active theme. */
export function consoleColours(): Record<string, string> {
  const isDark = dark();
  const map = ansi[isDark ? "indus-dark" : "indus-light"];
  const background = isDark ? "#000000" : "#f5f8fc";
  const foreground = isDark ? "#f0e6d2" : "#170d02";
  return {
    ...map,
    background,
    foreground,
    cursor: foreground,
    cursorAccent: background,
    selectionBackground: isDark ? "#3a3a3a" : "#c8d8f0",
  };
}

export const createConsoleTerminal: ConsoleTerminalFactory = async () => {
  const [{ Terminal }, { FitAddon }, { Unicode11Addon }] = await Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-fit"),
    import("@xterm/addon-unicode11"),
    import("@xterm/xterm/css/xterm.css"),
  ]);
  const mono = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim();
  const term = new Terminal({
    allowProposedApi: true, // the Unicode 11 width table is a proposed API
    cursorBlink: true,
    fontFamily: mono || "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 13,
    lineHeight: 1.25,
    scrollback: 3_000,
    theme: consoleColours(),
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new Unicode11Addon());
  term.unicode.activeVersion = "11";
  const restyle = new MutationObserver(() => {
    term.options.theme = consoleColours();
  });
  restyle.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return {
    open: (parent) => term.open(parent),
    write: (text) => term.write(text),
    onData: (listener) => {
      const sub = term.onData(listener);
      return () => sub.dispose();
    },
    fit: () => {
      try {
        fit.fit();
      } catch {
        // parent not laid out yet
      }
    },
    focus: () => term.focus(),
    dispose: () => {
      restyle.disconnect();
      term.dispose();
    },
  };
};
