// The load-time check of a desktop plugin's source (P24-32). A plugin is one ESM file; before the
// renderer may evaluate it, main reads it once and decides:
//
//   - imports: only the specifiers in `ALLOWED_IMPORTS` (the SDK and React). Relative paths, URLs,
//     `node:` and other packages, `export … from` of anything else, and any dynamic `import(…)`
//     are refused, so a plugin cannot pull in a second stage;
//   - names: a code word in `DENIED_NAMES` (the desktop bridge, Node's `require`/`process`, code
//     from text) refuses the file. This is a tripwire, not a sandbox (R07.19);
//   - no JSX: a `<Tag` where an expression starts is refused (plugins are plain ESM).
//
// Words inside strings, comments, template text and regex literals are ignored: the scanner first
// classifies every character (`classify`), and only code characters count. The accepted text is
// rewritten so the allowed specifiers point at the host's shim modules (`rewriteImports`); nothing
// else in the file changes.

export const ALLOWED_IMPORTS: ReadonlySet<string> = new Set([
  "@indus/desktop-sdk",
  "react",
  "react/jsx-runtime",
]);

/** Refused wherever they appear in code, also as a property (`window.indusDesktop`). */
export const DENIED_NAMES: ReadonlySet<string> = new Set([
  "indusDesktop",
  "importScripts",
  "WebAssembly",
  "__proto__",
]);

/** Refused as a free name (`eval(…)`, `new Function(…)`, `require(…)`, `process.env`), allowed as
 * someone else's property (`list.process`). */
export const DENIED_FREE_NAMES: ReadonlySet<string> = new Set([
  "eval",
  "Function",
  "require",
  "process",
]);

/** What each character of the source is. */
export const Kind = {
  Code: 0,
  Str: 1,
  Comment: 2,
  Template: 3,
  Regex: 4,
} as const;
export type Kind = (typeof Kind)[keyof typeof Kind];

const WORD = /[A-Za-z0-9_$]/;
const KEYWORDS_BEFORE_EXPRESSION = new Set([
  "return",
  "typeof",
  "instanceof",
  "in",
  "of",
  "new",
  "delete",
  "void",
  "throw",
  "case",
  "do",
  "else",
  "yield",
  "await",
]);

/** The word that ends at `end` (exclusive) in `source`, or "". */
function wordBefore(source: string, end: number): string {
  let start = end;
  while (start > 0 && WORD.test(source[start - 1] ?? "")) start -= 1;
  return source.slice(start, end);
}

/** Whether a `/` at `at` starts a regex literal, judged by the last code character before it. */
function slashStartsRegex(source: string, kinds: Uint8Array, at: number): boolean {
  let j = at - 1;
  while (j >= 0 && (kinds[j] !== Kind.Code || /\s/.test(source[j] ?? ""))) j -= 1;
  if (j < 0) return true;
  const prev = source[j] ?? "";
  if (prev === ")" || prev === "]" || prev === "}") return prev === "}";
  if (kinds[j] === Kind.Str || kinds[j] === Kind.Template) return false;
  if (WORD.test(prev)) {
    const word = wordBefore(source, j + 1);
    const dot = source[j - word.length] === ".";
    return !dot && KEYWORDS_BEFORE_EXPRESSION.has(word);
  }
  if (prev === "+" || prev === "-") return source[j - 1] !== prev;
  return true;
}

/** One `Kind` per character of `source`. */
export function classify(source: string): Uint8Array {
  const kinds = new Uint8Array(source.length);
  const braces: Array<"code" | "template"> = [];
  let i = 0;
  const mark = (from: number, to: number, kind: Kind) => kinds.fill(kind, from, to);

  const templateText = (from: number): number => {
    // from points just after a backtick or a `}` that closes `${…}`; returns where code resumes
    let j = from;
    while (j < source.length) {
      const c = source[j];
      if (c === "\\") {
        j += 2;
        continue;
      }
      if (c === "`") {
        mark(from - 1, j + 1, Kind.Template);
        return j + 1;
      }
      if (c === "$" && source[j + 1] === "{") {
        mark(from - 1, j + 2, Kind.Template);
        braces.push("template");
        return j + 2;
      }
      j += 1;
    }
    mark(from - 1, source.length, Kind.Template);
    return source.length;
  };

  while (i < source.length) {
    const c = source[i];
    const next = source[i + 1];
    if (c === "/" && next === "/") {
      const end = source.indexOf("\n", i);
      const stop = end < 0 ? source.length : end;
      mark(i, stop, Kind.Comment);
      i = stop;
    } else if (c === "/" && next === "*") {
      const end = source.indexOf("*/", i + 2);
      const stop = end < 0 ? source.length : end + 2;
      mark(i, stop, Kind.Comment);
      i = stop;
    } else if (c === "'" || c === '"') {
      let j = i + 1;
      while (j < source.length && source[j] !== c && source[j] !== "\n") {
        j += source[j] === "\\" ? 2 : 1;
      }
      const stop = Math.min(j + 1, source.length);
      mark(i, stop, Kind.Str);
      i = stop;
    } else if (c === "`") {
      i = templateText(i + 1);
    } else if (c === "{") {
      braces.push("code");
      i += 1;
    } else if (c === "}") {
      const top = braces.pop();
      i = top === "template" ? templateText(i + 1) : i + 1;
    } else if (c === "/" && slashStartsRegex(source, kinds, i)) {
      let j = i + 1;
      let inClass = false;
      while (j < source.length && source[j] !== "\n") {
        const r = source[j];
        if (r === "\\") {
          j += 2;
          continue;
        }
        if (r === "[") inClass = true;
        else if (r === "]") inClass = false;
        else if (r === "/" && !inClass) break;
        j += 1;
      }
      if (j >= source.length || source[j] !== "/") {
        i += 1; // not a closed regex on this line: a division after all
        continue;
      }
      j += 1;
      while (j < source.length && /[a-z]/i.test(source[j] ?? "")) j += 1;
      mark(i, j, Kind.Regex);
      i = j;
    } else {
      i += 1;
    }
  }
  return kinds;
}

export interface ImportUse {
  specifier: string;
  /** Offsets of the specifier's text (without quotes). */
  start: number;
  end: number;
}

export type ScanResult = { ok: true; imports: ImportUse[] } | { ok: false; reason: string };

function skipSpace(source: string, kinds: Uint8Array, from: number): number {
  let j = from;
  while (j < source.length && (kinds[j] === Kind.Comment || /\s/.test(source[j] ?? ""))) j += 1;
  return j;
}

/** The string literal starting at `at`, if one does. */
function stringAt(source: string, kinds: Uint8Array, at: number): ImportUse | null {
  const quote = source[at];
  if ((quote !== "'" && quote !== '"') || kinds[at] !== Kind.Str) return null;
  let end = at + 1;
  while (end < source.length && kinds[end] === Kind.Str && source[end] !== quote) end += 1;
  return { specifier: source.slice(at + 1, end), start: at + 1, end };
}

function codeWords(source: string, kinds: Uint8Array): Array<{ word: string; at: number }> {
  const out: Array<{ word: string; at: number }> = [];
  const pattern = /[A-Za-z_$][A-Za-z0-9_$]*/g;
  for (const match of source.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (kinds[at] !== Kind.Code) continue;
    if (at > 0 && WORD.test(source[at - 1] ?? "") && kinds[at - 1] === Kind.Code) continue;
    out.push({ word: match[0], at });
  }
  return out;
}

/** Decide whether `source` may load (see the module notes). */
export function scanPluginSource(source: string): ScanResult {
  const kinds = classify(source);
  const imports: ImportUse[] = [];
  for (const { word, at } of codeWords(source, kinds)) {
    const after = skipSpace(source, kinds, at + word.length);
    const prevChar = source[at - 1];
    const member = prevChar === "." && kinds[at - 1] === Kind.Code;
    if (DENIED_NAMES.has(word) || (DENIED_FREE_NAMES.has(word) && !member)) {
      return { ok: false, reason: `uses \`${word}\`, which plugins may not reach` };
    }
    if (word === "import" && !member) {
      if (source[after] === "(") return { ok: false, reason: "dynamic import() is not allowed" };
      if (source[after] === ".") return { ok: false, reason: "import.meta is not available" };
      const bare = stringAt(source, kinds, after);
      if (bare !== null) imports.push(bare);
    } else if (word === "from" && !member) {
      const spec = stringAt(source, kinds, after);
      if (spec !== null) imports.push(spec);
    }
  }
  const refused = imports.find((use) => !ALLOWED_IMPORTS.has(use.specifier));
  if (refused !== undefined) {
    return {
      ok: false,
      reason: `imports ${JSON.stringify(refused.specifier)}; plugins may import only ${[
        ...ALLOWED_IMPORTS,
      ].join(", ")}`,
    };
  }
  const jsx = /(^|[=(,:?&|!{}[;>]|return)\s*<[A-Za-z]/m;
  for (const match of source.matchAll(new RegExp(jsx.source, "gm"))) {
    const lt = (match.index ?? 0) + match[0].lastIndexOf("<");
    if (kinds[lt] === Kind.Code) return { ok: false, reason: "JSX is not supported; use plain JS" };
  }
  return { ok: true, imports };
}

/** `source` with each allowed import pointed at its shim URL (offsets from `scanPluginSource`). */
export function rewriteImports(
  source: string,
  imports: readonly ImportUse[],
  shimFor: (specifier: string) => string,
): string {
  let out = "";
  let last = 0;
  for (const use of [...imports].sort((a, b) => a.start - b.start)) {
    out += source.slice(last, use.start) + shimFor(use.specifier);
    last = use.end;
  }
  return out + source.slice(last);
}
