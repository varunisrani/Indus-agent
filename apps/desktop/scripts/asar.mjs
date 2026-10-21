// A minimal reader for the asar archive header (P20-38), enough to list what a packaged app holds
// without another dependency. Layout: a 4-byte size pickle (uint32 LE = 4), the header pickle's
// size (uint32 LE), then the header pickle: payload size (uint32 LE), JSON length (uint32 LE) and
// the JSON text `{ files: { name: entry } }`, where an entry is a folder (`files`), a link (`link`)
// or a file (`size`, `offset`, optionally `unpacked: true`).

import fs from "node:fs";

/** Parse the header JSON from the first bytes of an archive. */
export function parseAsarHeader(buffer) {
  if (buffer.length < 16) throw new Error("asar: file too short");
  if (buffer.readUInt32LE(0) !== 4) throw new Error("asar: bad size pickle");
  const headerSize = buffer.readUInt32LE(4);
  const jsonLength = buffer.readUInt32LE(12);
  if (16 + jsonLength > 8 + headerSize || 16 + jsonLength > buffer.length) {
    throw new Error("asar: header length out of range");
  }
  return JSON.parse(buffer.toString("utf8", 16, 16 + jsonLength));
}

/** Read the header of the archive at `file` (reads only the header bytes). */
export function readAsarHeader(file) {
  const fd = fs.openSync(file, "r");
  try {
    const head = Buffer.alloc(8);
    if (fs.readSync(fd, head, 0, 8, 0) !== 8) throw new Error("asar: file too short");
    const headerSize = head.readUInt32LE(4);
    if (headerSize > 256 * 1024 * 1024) throw new Error("asar: header too large");
    const buffer = Buffer.alloc(8 + headerSize);
    fs.readSync(fd, buffer, 0, buffer.length, 0);
    return parseAsarHeader(buffer);
  } finally {
    fs.closeSync(fd);
  }
}

/** Flatten a header into `[{ path, kind: "file" | "link", unpacked }]` with `/` separators. */
export function listAsar(header) {
  const out = [];
  const walk = (files, prefix) => {
    for (const [name, entry] of Object.entries(files ?? {})) {
      const full = prefix === "" ? name : `${prefix}/${name}`;
      if (entry && typeof entry === "object" && "files" in entry) walk(entry.files, full);
      else if (entry && typeof entry === "object" && "link" in entry)
        out.push({ path: full, kind: "link", unpacked: false });
      else out.push({ path: full, kind: "file", unpacked: entry?.unpacked === true });
    }
  };
  walk(header.files, "");
  return out;
}

/** Build an archive (header + contents) from `{ path: string | { unpacked: true } }` — for tests. */
export function buildAsar(entries) {
  const root = { files: {} };
  const chunks = [];
  let offset = 0;
  for (const [file, content] of Object.entries(entries)) {
    const parts = file.split("/");
    let dir = root;
    for (const part of parts.slice(0, -1)) {
      dir.files[part] ??= { files: {} };
      dir = dir.files[part];
    }
    const name = parts[parts.length - 1];
    if (typeof content === "string") {
      const bytes = Buffer.from(content, "utf8");
      dir.files[name] = { size: bytes.length, offset: String(offset) };
      chunks.push(bytes);
      offset += bytes.length;
    } else {
      dir.files[name] = { size: 0, unpacked: true };
    }
  }
  const json = Buffer.from(JSON.stringify(root), "utf8");
  const padded = Math.ceil(json.length / 4) * 4;
  const header = Buffer.alloc(16 + padded);
  header.writeUInt32LE(4, 0);
  header.writeUInt32LE(8 + padded, 4);
  header.writeUInt32LE(4 + padded, 8);
  header.writeUInt32LE(json.length, 12);
  json.copy(header, 16);
  return Buffer.concat([header, ...chunks]);
}
