// Draw the app icon (P20-38, N27: our own artwork). A rounded tile in the accent teal with three
// white river waves, painted from signed-distance shapes with supersampling and written as PNGs
// by a tiny encoder (no image library):
//
//   resources/icons/icon.png            1024 × 1024 (macOS, Windows; electron-builder converts)
//   resources/icons/png/<n>x<n>.png     16 … 512 (Linux AppImage, the runtime launcher entry)
//
//   node scripts/make-icons.mjs [--out DIR]

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const LINUX_SIZES = Object.freeze([16, 32, 48, 64, 128, 256, 512]);

// @indus/ui accent (light and dark themes) and the white mark.
const TOP = [0x00, 0x7c, 0x9a];
const BOTTOM = [0x3d, 0xb6, 0xda];

// -- PNG -------------------------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (const byte of bytes) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "ascii");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, crc]);
}

/** Encode RGBA pixels (`Uint8Array`, width × height × 4) as a PNG. */
export function encodePng(width, height, rgba) {
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    raw[y * (width * 4 + 1)] = 0; // filter: none
    Buffer.from(rgba.buffer, rgba.byteOffset + y * width * 4, width * 4).copy(
      raw,
      y * (width * 4 + 1) + 1,
    );
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Width and height from a PNG's IHDR, or null when the bytes are not a PNG. */
export function pngSize(bytes) {
  if (bytes.length < 24 || bytes.readUInt32BE(0) !== 0x89504e47) return null;
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

// -- shapes (in a 1024-unit design space) ----------------------------------------------------------

function roundedRect(x, y) {
  const half = 412; // tile 824 wide, centred
  const radius = 184;
  const qx = Math.abs(x - 512) - (half - radius);
  const qy = Math.abs(y - 512) - (half - radius);
  const outside = Math.hypot(Math.max(qx, 0), Math.max(qy, 0));
  return outside + Math.min(Math.max(qx, qy), 0) - radius;
}

const WAVES = [
  { y: 392, alpha: 1 },
  { y: 512, alpha: 0.86 },
  { y: 632, alpha: 0.72 },
];
const WAVE_FROM = 286;
const WAVE_TO = 738;
const AMPLITUDE = 34;
const WAVELENGTH = 226;
const STROKE = 30; // half-width

// Both ends sit on a crest (flat), so the round caps meet the stroke without a bulge.
const phase = (x) => ((x - WAVE_FROM) / WAVELENGTH) * 2 * Math.PI;

function waveY(baseY, x) {
  return baseY + AMPLITUDE * Math.cos(phase(x));
}

/** Distance from (x, y) to one wave stroke with round caps (negative inside). */
function wave(baseY, x, y) {
  const cx = Math.min(Math.max(x, WAVE_FROM), WAVE_TO);
  const fy = waveY(baseY, cx);
  if (cx !== x) return Math.hypot(x - cx, y - fy) - STROKE;
  const slope = -AMPLITUDE * ((2 * Math.PI) / WAVELENGTH) * Math.sin(phase(x));
  return Math.abs(y - fy) / Math.sqrt(1 + slope * slope) - STROKE;
}

/** Colour of one design-space point: [r, g, b, a] in 0..1. */
function shade(x, y, pixel) {
  const tile = roundedRect(x, y);
  if (tile > pixel) return [0, 0, 0, 0];
  const t = Math.min(Math.max((y - 100) / 824, 0), 1);
  let rgb = TOP.map((c, i) => (c + (BOTTOM[i] - c) * t) / 255);
  for (const w of WAVES) {
    const d = wave(w.y, x, y);
    const cover = Math.min(Math.max(0.5 - d / pixel, 0), 1) * w.alpha;
    if (cover > 0) rgb = rgb.map((c) => c + (1 - c) * cover);
  }
  const edge = Math.min(Math.max(0.5 - tile / pixel, 0), 1);
  return [rgb[0], rgb[1], rgb[2], edge];
}

/** Render the icon at `size` px with `samples`² supersampling. */
export function renderIcon(size, samples = 4) {
  const out = new Uint8Array(size * size * 4);
  const scale = 1024 / size;
  for (let py = 0; py < size; py += 1) {
    for (let px = 0; px < size; px += 1) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < samples; sy += 1) {
        for (let sx = 0; sx < samples; sx += 1) {
          const x = (px + (sx + 0.5) / samples) * scale;
          const y = (py + (sy + 0.5) / samples) * scale;
          const [cr, cg, cb, ca] = shade(x, y, scale / samples);
          r += cr * ca;
          g += cg * ca;
          b += cb * ca;
          a += ca;
        }
      }
      const i = (py * size + px) * 4;
      const n = samples * samples;
      out[i + 3] = Math.round((a / n) * 255);
      if (a > 0) {
        out[i] = Math.round((r / a) * 255);
        out[i + 1] = Math.round((g / a) * 255);
        out[i + 2] = Math.round((b / a) * 255);
      }
    }
  }
  return out;
}

export function writeIcons(outDir = path.join(APP_DIR, "resources", "icons")) {
  fs.mkdirSync(path.join(outDir, "png"), { recursive: true });
  const written = [];
  const put = (file, size) => {
    fs.writeFileSync(file, encodePng(size, size, renderIcon(size)));
    written.push(file);
  };
  put(path.join(outDir, "icon.png"), 1024);
  for (const size of LINUX_SIZES) put(path.join(outDir, "png", `${size}x${size}.png`), size);
  return written;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const at = process.argv.indexOf("--out");
  const written = writeIcons(at > 0 ? path.resolve(process.argv[at + 1] ?? "") : undefined);
  for (const file of written) process.stdout.write(`wrote ${path.relative(process.cwd(), file)}\n`);
}
