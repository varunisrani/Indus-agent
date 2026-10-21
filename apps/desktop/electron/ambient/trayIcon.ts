// The tray glyph (P20-36), drawn in code so the tray needs no image file: a ring with an upright
// bar through its middle, anti-aliased by supersampling. macOS gets a black template image (the
// menu bar tints it); other platforms get our accent colour. Pixels are BGRA, the order
// Electron's `nativeImage.createFromBitmap` reads. A packaged icon (P20-38), when given, wins.

export interface TrayBitmap {
  width: number;
  height: number;
  /** BGRA, row-major, `width * height * 4` bytes. */
  pixels: Uint8Array;
  /** macOS: draw as a template (the system picks the colour). */
  template: boolean;
}

/** Our accent (`--indus-color-accent`, dark theme) as [r, g, b]. */
const ACCENT: readonly [number, number, number] = [0x3d, 0xb6, 0xda];
const SAMPLES = 4;

function coverage(px: number, py: number, size: number): number {
  const c = size / 2;
  const outer = size * 0.44;
  const inner = size * 0.32;
  const barHalfWidth = size * 0.07;
  const barHalfHeight = size * 0.22;
  let hits = 0;
  for (let sy = 0; sy < SAMPLES; sy += 1) {
    for (let sx = 0; sx < SAMPLES; sx += 1) {
      const x = px + (sx + 0.5) / SAMPLES - c;
      const y = py + (sy + 0.5) / SAMPLES - c;
      const r = Math.hypot(x, y);
      const ring = r <= outer && r >= inner;
      const bar = Math.abs(x) <= barHalfWidth && Math.abs(y) <= barHalfHeight;
      if (ring || bar) hits += 1;
    }
  }
  return hits / (SAMPLES * SAMPLES);
}

export function trayBitmap(platform: string, scale = 1): TrayBitmap {
  const base = platform === "darwin" ? 18 : 22;
  const size = Math.max(8, Math.round(base * scale));
  const template = platform === "darwin";
  const [r, g, b] = template ? [0, 0, 0] : ACCENT;
  const pixels = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const alpha = Math.round(coverage(x, y, size) * 255);
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
