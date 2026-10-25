// The tray glyph (P20-36), drawn in code so the tray needs no image file: the brand mark (W63,
// `../brandMark.ts`), anti-aliased. macOS gets a black template image (the menu bar tints it);
// other platforms get the brand ember. Pixels are BGRA, the order Electron's
// `nativeImage.createFromBitmap` reads. A packaged icon (P20-38), when given, wins.

import { BRAND_COLORS } from "@indus/ui";
import { markCoverage } from "../brandMark.ts";

export interface TrayBitmap {
  width: number;
  height: number;
  /** BGRA, row-major, `width * height * 4` bytes. */
  pixels: Uint8Array;
  /** macOS: draw as a template (the system picks the colour). */
  template: boolean;
}

/** The brand ember (`@indus/ui` BRAND_COLORS.ember) as [r, g, b]. */
const EMBER = hexRgb(BRAND_COLORS.ember);
/** The mark's box fills this much of the tray square (a little air for the panel). */
const FILL = 0.9;

function hexRgb(hex: string): readonly [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function trayBitmap(platform: string, scale = 1): TrayBitmap {
  const base = platform === "darwin" ? 18 : 22;
  const size = Math.max(8, Math.round(base * scale));
  const template = platform === "darwin";
  const [r, g, b] = template ? [0, 0, 0] : EMBER;
  const cover = markCoverage(size, FILL, 16);
  const pixels = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const alpha = Math.round((cover[y * size + x] as number) * 255);
      const at = (y * size + x) * 4;
      // Premultiplied BGRA (what Chromium's bitmaps hold).
      pixels[at] = Math.round((b * alpha) / 255);
      pixels[at + 1] = Math.round((g * alpha) / 255);
      pixels[at + 2] = Math.round((r * alpha) / 255);
      pixels[at + 3] = alpha;
    }
  }
  return { width: size, height: size, pixels, template };
}
