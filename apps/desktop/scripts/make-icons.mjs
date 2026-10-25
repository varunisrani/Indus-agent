// Draw the app icon (P20-38, N27: our own artwork; W63: the Indus AGI brand). A rounded tile in the
// brand ember with the white mark inset as in `packages/ui/brand/app-icon.svg` (the mark's 100
// units in a 160-unit tile), filled from `@indus/ui` MARK_PATH by electron/brandMark.ts (exact
// row coverage, plain arithmetic, so the bytes are reproducible) and written as PNGs by a tiny
// encoder (no image library):
//
//   resources/icons/icon.png            1024 × 1024 (macOS, Windows; electron-builder converts)
//   resources/icons/png/<n>x<n>.png     16 … 512 (Linux AppImage, the runtime launcher entry)
//
//   node scripts/make-icons.mjs [--out DIR]

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";
import { BRAND_COLORS } from "@indus/ui";
import { coverage, MARK_BOX, mark } from "../electron/brandMark.ts";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const LINUX_SIZES = Object.freeze([16, 32, 48, 64, 128, 256, 512]);

const rgb = (hex) => [1, 3, 5].map((at) => Number.parseInt(hex.slice(at, at + 2), 16));
const TILE_RGB = rgb(BRAND_COLORS.ember);
const MARK_RGB = rgb(BRAND_COLORS.white);

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

const TILE = 824; // centred, the macOS grid's margin around it
const TILE_RADIUS = 184;
const TILE_FROM = (1024 - TILE) / 2;
/** app-icon.svg: the mark's 100 units inset 30 in a 160-unit tile. */
const MARK_SIZE = (TILE * MARK_BOX) / (MARK_BOX + 2 * 30);
const MARK_FROM = (1024 - MARK_SIZE) / 2;
const CORNER_STEPS = 32;

/** The tile's outline: a rounded square, its quarter circles flattened. */
function tilePolygon() {
  const points = [];
  const lo = TILE_FROM + TILE_RADIUS;
  const hi = TILE_FROM + TILE - TILE_RADIUS;
  const corners = [
    [hi, hi, 0],
    [lo, hi, 0.5],
    [lo, lo, 1],
    [hi, lo, 1.5],
  ];
  for (const [cx, cy, turn] of corners) {
    for (let i = 0; i <= CORNER_STEPS; i += 1) {
      const angle = (turn + (i / CORNER_STEPS) * 0.5) * Math.PI;
      points.push([cx + TILE_RADIUS * Math.cos(angle), cy + TILE_RADIUS * Math.sin(angle)]);
    }
  }
  return points;
}

/** Render the icon at `size` px as straight RGBA (`rows` coverage samples per pixel row). */
export function renderIcon(size, rows = size >= 256 ? 4 : 16) {
  const unit = size / 1024;
  const tile = coverage([tilePolygon()], size, size, { scale: unit, dx: 0, dy: 0 }, rows);
  const markScale = (MARK_SIZE / MARK_BOX) * unit;
  const shape = coverage(
    mark(),
    size,
    size,
    { scale: markScale, dx: MARK_FROM * unit, dy: MARK_FROM * unit },
    rows,
  );
  const out = new Uint8Array(size * size * 4);
  for (let i = 0; i < size * size; i += 1) {
    const a = tile[i];
    if (a <= 0) continue;
    // the mark lies inside the tile: its share of the painted part of the pixel
    const m = Math.min(1, shape[i] / a);
    for (let c = 0; c < 3; c += 1) {
      out[i * 4 + c] = Math.round(TILE_RGB[c] + (MARK_RGB[c] - TILE_RGB[c]) * m);
    }
    out[i * 4 + 3] = Math.round(a * 255);
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
