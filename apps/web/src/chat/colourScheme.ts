// Light/dark reports for the program inside the Chat tab's terminal (W47). The embedded TUI picks
// its colours for a light or dark background once at start; when the dashboard switches theme,
// xterm's colours change under it and its text could become unreadable (light grey on the light
// background). The terminal therefore speaks the colour-scheme report protocol other terminals
// use: a program asks with `CSI ? 996 n` and gets `CSI ? 997 ; 1 n` (dark) or `CSI ? 997 ; 2 n`
// (light); after `CSI ? 2031 h` it is also told, unasked, whenever the scheme changes, until
// `CSI ? 2031 l`. The report is sent as terminal input, like any other terminal answer.

/** DEC private mode that turns change reports on. */
export const SCHEME_REPORT_MODE = 2031;
/** The DSR a program sends to ask for the current scheme. */
export const SCHEME_QUERY = 996;

/** The report for a light or dark scheme. */
export function schemeReport(light: boolean): string {
  return `\u001b[?997;${light ? 2 : 1}n`;
}

type Params = readonly (number | readonly number[])[];

function has(params: Params, value: number): boolean {
  return params.some((param) => (Array.isArray(param) ? param.includes(value) : param === value));
}

/**
 * The terminal side of the protocol. `isLight` reads the scheme now; `send` writes terminal input
 * to the program. The handlers return whether they consumed the sequence (xterm's parser hooks).
 */
export class SchemeReporter {
  readonly #isLight: () => boolean;
  readonly #send: (data: string) => void;
  #notify = false;
  #last: boolean | null = null;

  constructor(isLight: () => boolean, send: (data: string) => void) {
    this.#isLight = isLight;
    this.#send = send;
  }

  get notifying(): boolean {
    return this.#notify;
  }

  /** `CSI ? Ps n`: answers the scheme query; any other DSR is left to the terminal. */
  deviceStatus(params: Params): boolean {
    if (!has(params, SCHEME_QUERY)) return false;
    this.#report();
    return true;
  }

  /** `CSI ? Pm h` / `l`: notes mode 2031; never consumes (other modes in the list still apply). */
  setMode(params: Params, on: boolean): boolean {
    if (has(params, SCHEME_REPORT_MODE)) this.#notify = on;
    return false;
  }

  /** The page's theme may have changed: report it when asked to and it really changed. */
  themeChanged(): void {
    if (!this.#notify) return;
    if (this.#isLight() === this.#last) return;
    this.#report();
  }

  /** A program started afresh (a new child): forget its mode. */
  reset(): void {
    this.#notify = false;
    this.#last = null;
  }

  #report(): void {
    const light = this.#isLight();
    this.#last = light;
    this.#send(schemeReport(light));
  }
}
