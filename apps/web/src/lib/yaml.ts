// A small YAML writer for the Config page's YAML view (W52-pc-10; saved back since W58-J2-2, so
// what it writes must read back as the same values under the server's YAML 1.1 reader). It covers
// what a config tree holds: maps, lists, strings, numbers, booleans and null, two spaces per level.
// Strings are double-quoted (JSON escapes are valid YAML) whenever plain style could be misread.

import { setPath } from "./nested.ts";
import type { ConfigObject, ConfigValue } from "./restTypes.ts";

const INDENT = "  ";

function isMap(value: ConfigValue): value is ConfigObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Words a YAML reader would turn into a boolean or null. */
const SPECIAL_WORDS = /^(true|false|yes|no|on|off|y|n|null|~)$/i;
const NUMBER_LIKE =
  /^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$|^0x[0-9a-f]+$|^0o[0-7]+$|^[-+]?\.(inf|nan)$/i;
const RISKY_START = /^[-?:,[\]{}#&*!|>'"%@`\s]/;
/** YAML 1.1 dates and timestamps (`2026-10-25`) and base-60 numbers (`1:30`), the merge key. */
const YAML11_SCALAR =
  /^\d{4}-\d\d?-\d\d?([Tt\s].*)?$|^[-+]?\d[\d_]*(:[0-5]?\d)+(\.[\d_]*)?$|^(<<|=)$/;

/** True when `text` must be quoted to read back as the same string. */
export function needsQuotes(text: string): boolean {
  if (text === "") return true;
  if (text !== text.trim()) return true;
  if (SPECIAL_WORDS.test(text) || NUMBER_LIKE.test(text) || YAML11_SCALAR.test(text)) return true;
  if (RISKY_START.test(text)) return true;
  if (text.includes(": ") || text.includes(" #") || text.endsWith(":")) return true;
  // biome-ignore lint/suspicious/noControlCharactersInRegex: control characters force quoting
  return /[\u0000-\u001f\u007f]/.test(text);
}

function scalar(value: ConfigValue): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number")
    return Number.isFinite(value) ? String(value) : JSON.stringify(String(value));
  if (typeof value === "string") return needsQuotes(value) ? JSON.stringify(value) : value;
  return JSON.stringify(value);
}

function key(name: string): string {
  return needsQuotes(name) ? JSON.stringify(name) : name;
}

function writeMap(map: ConfigObject, depth: number, out: string[]): void {
  const pad = INDENT.repeat(depth);
  for (const [name, value] of Object.entries(map)) {
    if (isMap(value)) {
      if (Object.keys(value).length === 0) {
        out.push(`${pad}${key(name)}: {}`);
      } else {
        out.push(`${pad}${key(name)}:`);
        writeMap(value, depth + 1, out);
      }
    } else if (Array.isArray(value)) {
      if (value.length === 0) {
        out.push(`${pad}${key(name)}: []`);
      } else {
        out.push(`${pad}${key(name)}:`);
        writeList(value, depth + 1, out);
      }
    } else {
      out.push(`${pad}${key(name)}: ${scalar(value)}`);
    }
  }
}

function writeList(list: readonly ConfigValue[], depth: number, out: string[]): void {
  const pad = INDENT.repeat(depth);
  for (const item of list) {
    if (isMap(item) && Object.keys(item).length > 0) {
      // The first pair sits on the dash line; the rest line up under it.
      const nested: string[] = [];
      writeMap(item, depth + 1, nested);
      const [first = "", ...rest] = nested;
      out.push(`${pad}- ${first.trimStart()}`, ...rest);
    } else if (Array.isArray(item) && item.length > 0) {
      const nested: string[] = [];
      writeList(item, depth + 1, nested);
      const [first = "", ...rest] = nested;
      out.push(`${pad}- ${first.trimStart()}`, ...rest);
    } else if (isMap(item)) {
      out.push(`${pad}- {}`);
    } else if (Array.isArray(item)) {
      out.push(`${pad}- []`);
    } else {
      out.push(`${pad}- ${scalar(item)}`);
    }
  }
}

/** The tree as YAML text, ending with a newline; an empty tree gives an empty string. */
export function toYaml(tree: ConfigObject): string {
  const out: string[] = [];
  writeMap(tree, 0, out);
  return out.length ? `${out.join("\n")}\n` : "";
}

/** Dotted-key entries (`display.bell`) nested into one tree, in the order given. */
export function nestEntries(entries: readonly { key: string; value: ConfigValue }[]): ConfigObject {
  let tree: ConfigObject = {};
  for (const entry of entries) tree = setPath(tree, entry.key, entry.value);
  return tree;
}
