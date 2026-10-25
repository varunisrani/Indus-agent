// The terminal pane's screen (P20-31): xterm, loaded on first use, with the WebGL renderer when
// the GPU allows it (falling back to xterm's DOM renderer when WebGL is missing or its context is
// lost), Unicode 11 widths and a fit to the pane. No links addon: output never opens a URL.

import { ansi, type ThemeName } from "@indus/ui";
import type { ITheme } from "@xterm/xterm";

export interface TerminalView {
  open(element: HTMLElement): void;
  write(data: string): void;
  /** Keystrokes and pastes from the person. */
  onInput(listener: (data: string) => void): void;
  onSelectionChange(listener: (selection: string) => void): void;
  /** Size the screen to its element; answers the new grid. */
  fit(): { cols: number; rows: number };
  focus(): void;
  dispose(): void;
}

export type TerminalViewFactory = () => Promise<TerminalView>;

/** A CSS custom property's value on <html>, or the fallback before the tokens are painted. */
function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value === "" ? fallback : value;
}

/**
 * The screen's colours from the window's theme (W51-b): the canvas and text tokens, and the
 * matching ANSI set, so a light window gets a light terminal (it was black on white chrome).
 */
export function terminalTheme(): ITheme {
  const name: ThemeName =
    document.documentElement.dataset.theme === "indus-light" ? "indus-light" : "indus-dark";
  return {
    ...ansi[name],
    background: cssVar("--desk-bg-canvas", name === "indus-light" ? "#ffffff" : "#0d1015"),
    foreground: cssVar("--desk-fg-1", name === "indus-light" ? "#24292f" : "#d8dfe5"),
    cursor: cssVar("--desk-fg-1", name === "indus-light" ? "#24292f" : "#d8dfe5"),
    cursorAccent: cssVar("--desk-bg-canvas", name === "indus-light" ? "#ffffff" : "#0d1015"),
    selectionBackground: cssVar("--desk-selection", "rgba(74, 132, 254, 0.3)"),
  };
}

export const createXtermView: TerminalViewFactory = async () => {
  const [xterm, fitAddon, unicode, webgl] = await Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-fit"),
    import("@xterm/addon-unicode11"),
    import("@xterm/addon-webgl").catch(() => null),
    import("@xterm/xterm/css/xterm.css"),
  ]);
  const term = new xterm.Terminal({
    allowProposedApi: true,
    cursorBlink: true,
    fontFamily: "var(--indus-font-mono), monospace",
    fontSize: 13,
    scrollback: 5_000,
    theme: terminalTheme(),
  });
  // follow the window's light / dark switch (and an appearance preset repainting the tokens)
  const repaint = new MutationObserver(() => {
    term.options.theme = terminalTheme();
  });
  repaint.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const fit = new fitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new unicode.Unicode11Addon());
  term.unicode.activeVersion = "11";
  return {
    open: (element) => {
      term.open(element);
      if (webgl !== null) {
        try {
          const addon = new webgl.WebglAddon();
          addon.onContextLoss(() => addon.dispose());
          term.loadAddon(addon);
        } catch {
          // no WebGL here: the DOM renderer stays
        }
      }
    },
    write: (data) => term.write(data),
    onInput: (listener) => {
      term.onData(listener);
    },
    onSelectionChange: (listener) => {
      term.onSelectionChange(() => listener(term.getSelection()));
    },
    fit: () => {
      try {
        fit.fit();
      } catch {
        // not laid out yet
      }
      return { cols: term.cols, rows: term.rows };
    },
    focus: () => term.focus(),
    dispose: () => {
      repaint.disconnect();
      term.dispose();
    },
  };
};
