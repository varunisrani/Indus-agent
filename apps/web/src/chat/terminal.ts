// The chat's terminal view (P18-26): xterm.js with the fit, Unicode 11, web-links and WebGL addons,
// behind the small `ChatTerminal` surface the host needs (tests give the host a fake).
//
// WebGL is the fast renderer but not everywhere: software rasterisers (llvmpipe, SwiftShader,
// softpipe, anything reporting "software") advertise WebGL and then fail or crawl, so the DOM
// renderer is kept there; a context loss also falls back to it. Browsers cap live GL contexts, so
// the WebGL addon is disposed while the terminal is hidden (another dashboard tab, or the browser
// tab in the background) and loaded again on return.
//
// Colours come from the @indus/ui tokens of the active theme (CSS variables plus the 16-colour ANSI
// map), re-read when `data-theme` on <html> changes.

import { ansi, type ThemeName } from "@indus/ui";

export interface TermSize {
  cols: number;
  rows: number;
}

export interface ChatTerminal {
  open(parent: HTMLElement): void;
  write(text: string): void;
  onData(listener: (data: string) => void): () => void;
  onResize(listener: (size: TermSize) => void): () => void;
  size(): TermSize;
  /** Resize to the parent's box (no-op while the parent is hidden). */
  fit(): void;
  focus(): void;
  scrollLines(lines: number): void;
  /** Pixel height of one row, for touch scrolling. */
  rowHeight(): number;
  /** Load or release the GPU renderer (when supported). */
  setRendererVisible(visible: boolean): void;
  /** The hidden input xterm types into (composition events come from it). */
  input(): HTMLTextAreaElement | undefined;
  dispose(): void;
}

export type TerminalFactory = () => Promise<ChatTerminal>;

const SOFTWARE_RENDERER = /llvmpipe|swiftshader|softpipe|software/i;

/** True for a GL renderer string that names a software rasteriser. */
export function isSoftwareRenderer(renderer: string): boolean {
  return SOFTWARE_RENDERER.test(renderer);
}

/** Whether the GPU renderer should be used on this page (probes one context, then frees it). */
export function webglUsable(doc: Document = document): boolean {
  let gl: WebGLRenderingContext | WebGL2RenderingContext | null = null;
  try {
    const canvas = doc.createElement("canvas");
    gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
  } catch {
    return false;
  }
  if (!gl) return false;
  let software = false;
  try {
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    if (info) software = isSoftwareRenderer(String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)));
  } catch {
    // renderer name hidden: trust the context we got
  }
  try {
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  } catch {
    // already gone
  }
  return !software;
}

function themeName(): ThemeName {
  const set = document.documentElement.dataset.theme;
  if (set === "indus-dark" || set === "indus-light") return set;
  const dark =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;
  return dark ? "indus-dark" : "indus-light";
}

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** xterm colours for the active theme. */
export function terminalColours(): Record<string, string> {
  const name = themeName();
  const map = ansi[name];
  const dark = name === "indus-dark";
  return {
    background: cssVar("--indus-color-code-bg", dark ? "#16181d" : "#f6f5f2"),
    foreground: cssVar("--indus-color-fg", dark ? "#e8e6e1" : "#1f1f1c"),
    cursor: cssVar("--indus-color-accent", dark ? "#8ab4f8" : "#2557c7"),
    selectionBackground: cssVar("--indus-color-selection", dark ? "#33415c" : "#cdd9f2"),
    black: map.black,
    red: map.red,
    green: map.green,
    yellow: map.yellow,
    blue: map.blue,
    magenta: map.magenta,
    cyan: map.cyan,
    white: map.white,
    brightBlack: map.brightBlack,
    brightRed: map.brightRed,
    brightGreen: map.brightGreen,
    brightYellow: map.brightYellow,
    brightBlue: map.brightBlue,
    brightMagenta: map.brightMagenta,
    brightCyan: map.brightCyan,
    brightWhite: map.brightWhite,
  };
}

/** The real terminal (xterm and its addons load with the chat chunk). */
export const createXtermTerminal: TerminalFactory = async () => {
  const [{ Terminal }, { FitAddon }, { Unicode11Addon }, { WebLinksAddon }, { WebglAddon }] =
    await Promise.all([
      import("@xterm/xterm"),
      import("@xterm/addon-fit"),
      import("@xterm/addon-unicode11"),
      import("@xterm/addon-web-links"),
      import("@xterm/addon-webgl"),
      import("@xterm/xterm/css/xterm.css"),
    ]);

  const term = new Terminal({
    allowProposedApi: true, // the Unicode 11 width table is a proposed API
    cursorBlink: true,
    fontFamily: cssVar("--indus-font-mono", "ui-monospace, SFMono-Regular, Menlo, monospace"),
    fontSize: 14,
    scrollback: 5_000,
    theme: terminalColours(),
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new Unicode11Addon());
  term.unicode.activeVersion = "11";
  term.loadAddon(
    new WebLinksAddon((_event, uri) => {
      window.open(uri, "_blank", "noopener,noreferrer");
    }),
  );

  const gpuAllowed = webglUsable();
  let gpu: InstanceType<typeof WebglAddon> | null = null;
  let opened = false;
  const releaseGpu = () => {
    const addon = gpu;
    gpu = null;
    try {
      addon?.dispose();
    } catch {
      // already released
    }
  };
  const loadGpu = () => {
    if (!gpuAllowed || gpu || !opened) return;
    try {
      const addon = new WebglAddon();
      addon.onContextLoss(() => releaseGpu());
      term.loadAddon(addon);
      gpu = addon;
    } catch {
      gpu = null; // the DOM renderer stays
    }
  };

  const themeWatch = new MutationObserver(() => {
    term.options.theme = terminalColours();
  });
  themeWatch.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  return {
    open(parent) {
      term.open(parent);
      opened = true;
      loadGpu();
    },
    write: (text) => term.write(text),
    onData(listener) {
      const sub = term.onData(listener);
      return () => sub.dispose();
    },
    onResize(listener) {
      const sub = term.onResize(listener);
      return () => sub.dispose();
    },
    size: () => ({ cols: term.cols, rows: term.rows }),
    fit() {
      const element = term.element;
      if (!element || element.offsetParent === null) return; // hidden: nothing to measure
      try {
        fit.fit();
      } catch {
        // not measurable yet
      }
    },
    focus: () => term.focus(),
    scrollLines: (lines) => term.scrollLines(lines),
    rowHeight() {
      const height = term.element?.clientHeight ?? 0;
      return term.rows > 0 && height > 0 ? height / term.rows : 17;
    },
    setRendererVisible(visible) {
      if (visible) loadGpu();
      else releaseGpu();
    },
    input: () => term.textarea,
    dispose() {
      themeWatch.disconnect();
      releaseGpu();
      term.dispose();
    },
  };
};

/** SGR mouse reports (`ESC [ < b ; x ; y M|m`); never forwarded to the PTY. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal streams are escape sequences
const MOUSE_REPORT = /^(?:\x1b\[<\d+;\d+;\d+[Mm])+$/;

export function isMouseReport(data: string): boolean {
  return MOUSE_REPORT.test(data);
}

/** How long a composition commit waits for xterm to deliver it, and how long echoes count. */
export const COMMIT_WAIT_MS = 20;
export const ECHO_MS = 80;

/**
 * IME composition safety net. xterm normally turns a composition commit into `onData`; some
 * browsers and soft keyboards do not, and some deliver it twice (a repeated `compositionend`, or
 * `onData` after the commit). `commit(text)` forwards `text` after a short wait unless xterm's data
 * already carried it; `filter(data)` strips an echo of a commit this guard forwarded.
 */
export class CompositionGuard {
  readonly #send: (data: string) => void;
  readonly #now: () => number;
  #pending: string | null = null;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #forwarded: { text: string; at: number } | null = null;
  #typed: { text: string; at: number } | null = null;

  constructor(send: (data: string) => void, now: () => number = Date.now) {
    this.#send = send;
    this.#now = now;
  }

  #recent(entry: { text: string; at: number } | null): entry is { text: string; at: number } {
    return entry !== null && this.#now() - entry.at <= ECHO_MS;
  }

  /** A `compositionend` with its committed text. */
  commit(text: string): void {
    if (!text) return;
    if (this.#recent(this.#typed) && this.#typed.text.endsWith(text)) return; // xterm had it
    if (this.#recent(this.#forwarded) && this.#forwarded.text === text) return; // fired twice
    this.#release();
    this.#pending = text;
    this.#timer = setTimeout(() => this.#release(), COMMIT_WAIT_MS);
  }

  /** xterm's `onData`; returns what is still worth sending. */
  filter(data: string): string {
    let out = data;
    if (!data.startsWith("\x1b") && this.#recent(this.#forwarded)) {
      const sent = this.#forwarded.text;
      if (data === sent) out = "";
      else if (data.startsWith(sent)) out = data.slice(sent.length);
    }
    if (out && !out.startsWith("\x1b")) {
      const carried = this.#recent(this.#typed) ? this.#typed.text : "";
      this.#typed = { text: (carried + out).slice(-256), at: this.#now() };
      if (this.#pending !== null && this.#typed.text.endsWith(this.#pending)) this.#cancel();
    }
    return out;
  }

  dispose(): void {
    this.#cancel();
  }

  #release(): void {
    const text = this.#pending;
    this.#cancel();
    if (text) {
      this.#forwarded = { text, at: this.#now() };
      this.#send(text);
    }
  }

  #cancel(): void {
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
    this.#pending = null;
  }
}
