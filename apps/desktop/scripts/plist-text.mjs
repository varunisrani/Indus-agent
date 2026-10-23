// Small edits to an XML property list kept as text (P23-15). Electron's own `Info.plist` and the
// one electron-builder writes are XML, so the two edits the signing path needs — a top-level
// string key and the ASAR integrity hash — are done in place without a plist library. Anything
// that is not the shape we expect is reported (null) instead of guessed.

/** Escape text for an XML element body. */
export function xmlText(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function plainRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Set `<key>name</key><string>value</string>` in the top-level dict: replaced in place when the
 * key holds a string already, otherwise added just before the dict closes.
 */
export function setTopLevelString(text, name, value) {
  const existing = new RegExp(
    `(<key>${plainRegExp(xmlText(name))}</key>\\s*<string>)([^<]*)(</string>)`,
  ).exec(text);
  if (existing) {
    const start = existing.index + existing[1].length;
    return `${text.slice(0, start)}${xmlText(value)}${text.slice(start + existing[2].length)}`;
  }
  const end = text.lastIndexOf("</plist>");
  const close = end < 0 ? -1 : text.lastIndexOf("</dict>", end);
  if (close < 0) throw new Error("Info.plist: no top-level dict");
  const entry = `\t<key>${xmlText(name)}</key>\n\t<string>${xmlText(value)}</string>\n`;
  return `${text.slice(0, close)}${entry}${text.slice(close)}`;
}

/** The string value of a top-level key, or null. */
export function topLevelString(text, name) {
  const found = new RegExp(
    `<key>${plainRegExp(xmlText(name))}</key>\\s*<string>([^<]*)</string>`,
  ).exec(text);
  return found ? (found[1] ?? null) : null;
}

/**
 * Inside `ElectronAsarIntegrity`, the entry for `archive` (for example `Resources/app.asar`):
 * `{ hash, at }` where `at` is the hash's offset in `text`, or null when there is no such entry.
 */
export function integrityEntry(text, archive) {
  const block = text.indexOf("<key>ElectronAsarIntegrity</key>");
  if (block < 0) return null;
  const entry = text.indexOf(`<key>${xmlText(archive)}</key>`, block);
  if (entry < 0) return null;
  const rest = text.slice(entry);
  const entryEnd = rest.indexOf("</dict>");
  const hash = /<key>hash<\/key>\s*<string>([^<]*)<\/string>/.exec(rest);
  if (!hash || entryEnd < 0 || hash.index > entryEnd) return null;
  const value = hash[1] ?? "";
  return {
    hash: value,
    at: entry + hash.index + hash[0].length - "</string>".length - value.length,
  };
}

/** `text` with the integrity hash of `archive` set to `hash`; null when the entry is missing. */
export function withIntegrityHash(text, archive, hash) {
  const found = integrityEntry(text, archive);
  if (found === null) return null;
  if (found.hash === hash) return text;
  return `${text.slice(0, found.at)}${hash}${text.slice(found.at + found.hash.length)}`;
}
