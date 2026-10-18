import { oklchToHex } from "./palette.ts";
import type { ThemeName } from "./semantic.ts";

export const ansiNames = [
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
  "brightBlack",
  "brightRed",
  "brightGreen",
  "brightYellow",
  "brightBlue",
  "brightMagenta",
  "brightCyan",
  "brightWhite",
] as const;
export type AnsiName = (typeof ansiNames)[number];
export type AnsiMap = Readonly<Record<AnsiName, string>>;

/** Hue and chroma for the six chromatic slots; lightness is chosen per theme. */
const CHROMATIC = {
  red: { h: 26, c: 0.17 },
  green: { h: 148, c: 0.14 },
  yellow: { h: 84, c: 0.14 },
  blue: { h: 250, c: 0.14 },
  magenta: { h: 334, c: 0.15 },
  cyan: { h: 205, c: 0.11 },
} as const;
type ChromaticName = keyof typeof CHROMATIC;

interface AnsiLevels {
  /** Lightness of the six normal colours. */
  readonly normal: number;
  /** Lightness of the six bright colours. */
  readonly bright: number;
  /** Lightness of black, bright black, white and bright white. */
  readonly greys: readonly [number, number, number, number];
}

const NEUTRAL_HUE = 72;

function buildAnsi(levels: AnsiLevels): AnsiMap {
  const grey = (l: number) => oklchToHex({ l, c: 0.01, h: NEUTRAL_HUE });
  const tone = (name: ChromaticName, l: number) => oklchToHex({ l, ...CHROMATIC[name] });
  const [black, brightBlack, white, brightWhite] = levels.greys;
  return {
    black: grey(black),
    red: tone("red", levels.normal),
    green: tone("green", levels.normal),
    yellow: tone("yellow", levels.normal),
    blue: tone("blue", levels.normal),
    magenta: tone("magenta", levels.normal),
    cyan: tone("cyan", levels.normal),
    white: grey(white),
    brightBlack: grey(brightBlack),
    brightRed: tone("red", levels.bright),
    brightGreen: tone("green", levels.bright),
    brightYellow: tone("yellow", levels.bright),
    brightBlue: tone("blue", levels.bright),
    brightMagenta: tone("magenta", levels.bright),
    brightCyan: tone("cyan", levels.bright),
    brightWhite: grey(brightWhite),
  };
}

/** Terminal 16-colour maps: light themes need darker inks, dark themes lighter ones. */
export const ansi: Readonly<Record<ThemeName, AnsiMap>> = {
  "indus-light": buildAnsi({ normal: 0.5, bright: 0.42, greys: [0.28, 0.5, 0.86, 0.97] }),
  "indus-dark": buildAnsi({ normal: 0.72, bright: 0.82, greys: [0.24, 0.48, 0.86, 0.98] }),
};
