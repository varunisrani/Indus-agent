// The one place window options are made. Every kind gets the same security floor — context
// isolation, renderer sandbox, no Node, web security on — and differs only where its job needs:
// chat windows may embed the preview `<webview>`, autoplay audio (voice) and never take focus
// from a navigation (R08.20); transparent kinds start transparent so no white frame flashes;
// helper windows get no preload at all. DevTools exist only in dev builds.

export type WindowKind = "main" | "secondary" | "instance" | "hud" | "quick" | "hidden";

export interface KindTraits {
  /** Renders the chat app (preload, IPC, sender-checked). */
  app: boolean;
  /** Hosts working panes, so it may embed the preview `<webview>`. */
  panes: boolean;
  transparent: boolean;
  /** Sizes itself from its content (zoom is not re-asserted there). */
  selfSizing: boolean;
  frameless: boolean;
}

export const KIND_TRAITS: Readonly<Record<WindowKind, KindTraits>> = {
  main: { app: true, panes: true, transparent: false, selfSizing: false, frameless: true },
  secondary: { app: true, panes: true, transparent: false, selfSizing: false, frameless: true },
  instance: { app: true, panes: true, transparent: false, selfSizing: false, frameless: true },
  hud: { app: true, panes: false, transparent: true, selfSizing: true, frameless: true },
  quick: { app: true, panes: false, transparent: true, selfSizing: true, frameless: true },
  hidden: { app: false, panes: false, transparent: false, selfSizing: false, frameless: false },
};

export const TITLE_BAR_HEIGHT = 34;
/** macOS traffic lights are 14 px tall; centred in the band, 16 px from the left edge. */
export const TRAFFIC_LIGHTS = { x: 16, y: (TITLE_BAR_HEIGHT - 14) / 2 } as const;
export const WINDOW_MIN_WIDTH = 400;
export const WINDOW_MIN_HEIGHT = 620;

export interface WebPreferencesShape {
  preload?: string;
  contextIsolation: true;
  sandbox: true;
  nodeIntegration: false;
  nodeIntegrationInSubFrames: false;
  nodeIntegrationInWorker: false;
  webSecurity: true;
  allowRunningInsecureContent: false;
  webviewTag: boolean;
  devTools: boolean;
  spellcheck: false;
  autoplayPolicy: "no-user-gesture-required" | "document-user-activation-required";
  focusOnNavigation: false;
  navigateOnDragDrop: false;
  safeDialogs: true;
  enableWebSQL: false;
}

export interface PreferenceInput {
  preloadPath: string;
  dev: boolean;
}

export function webPreferencesFor(kind: WindowKind, input: PreferenceInput): WebPreferencesShape {
  const traits = KIND_TRAITS[kind];
  const prefs: WebPreferencesShape = {
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    nodeIntegrationInSubFrames: false,
    nodeIntegrationInWorker: false,
    webSecurity: true,
    allowRunningInsecureContent: false,
    webviewTag: traits.panes,
    devTools: input.dev && traits.app,
    // Off until P20-39 picks the dictionary source: Chromium would otherwise fetch Hunspell
    // dictionaries from a vendor CDN at the first window (Linux, Windows).
    spellcheck: false,
    autoplayPolicy: traits.app ? "no-user-gesture-required" : "document-user-activation-required",
    focusOnNavigation: false,
    navigateOnDragDrop: false,
    safeDialogs: true,
    enableWebSQL: false,
  };
  if (traits.app) prefs.preload = input.preloadPath;
  return prefs;
}

export interface Bounds {
  x?: number;
  y?: number;
  width: number;
  height: number;
}

export interface TitleBarColors {
  color: string;
  symbolColor: string;
}

export interface WindowOptionsInput extends PreferenceInput {
  platform: string;
  bounds: Bounds;
  title: string;
  backgroundColor: string;
  titleBar: TitleBarColors;
  icon?: string;
}

/** Plain options for `new BrowserWindow(...)`; Electron's type is structurally compatible. */
export function windowOptionsFor(
  kind: WindowKind,
  input: WindowOptionsInput,
): Record<string, unknown> {
  const traits = KIND_TRAITS[kind];
  const options: Record<string, unknown> = {
    ...input.bounds,
    title: input.title,
    show: false,
    webPreferences: webPreferencesFor(kind, input),
  };
  if (input.icon !== undefined) options.icon = input.icon;
  if (kind !== "hidden" && !traits.selfSizing) {
    options.minWidth = WINDOW_MIN_WIDTH;
    options.minHeight = WINDOW_MIN_HEIGHT;
  }
  if (traits.transparent) {
    options.transparent = true;
    options.frame = false;
    options.backgroundColor = "#00000000";
    options.hasShadow = false;
  } else {
    options.backgroundColor = input.backgroundColor;
  }
  if (traits.frameless && !traits.transparent) {
    options.titleBarStyle = "hidden";
    if (input.platform === "darwin") {
      options.trafficLightPosition = { ...TRAFFIC_LIGHTS };
    } else {
      options.titleBarOverlay = { ...input.titleBar, height: TITLE_BAR_HEIGHT };
    }
  }
  return options;
}
