// Paste handling for the composer (P12-17, R05.19). A paste is cleaned, then — when it looks like
// a path — offered to the bridge as an image to stage or a dropped file; a long paste becomes a
// label token whose full text is what the model receives. Every await re-checks the destination
// and the composer revision (R05.10): a session switch drops the result, while typing in the
// meantime only means the raw text goes in as typed text.

import type { Rpc } from "../app/rpc.ts";
import type { Destination } from "../state/destination.ts";
import type { Composer, ComposerToken, ImageToken, PasteToken } from "./composerStore.ts";

const PASTE_OPEN = "\u001b[200~";
const PASTE_CLOSE = "\u001b[201~";

/** Bracketed-paste markers out, line endings unified, trailing blank lines dropped. */
export function cleanPaste(raw: string): string {
  const text = raw.split(PASTE_OPEN).join("").split(PASTE_CLOSE).join("").replace(/\r\n?/g, "\n");
  return /[^\n]/.test(text) ? text.replace(/\n+$/, "") : text;
}

/** A cheap local guess that a paste names a file; the bridge makes the real decision. */
export function looksLikePath(text: string): boolean {
  const word = text.trim();
  if (word === "" || word.includes("\n")) return false;
  const bare = word.replace(/^["']/, "");
  if (word.startsWith("file://")) return true;
  if (/^(~|\.{1,2})\//.test(bare)) return true;
  if (/^[A-Za-z]:[\\/]/.test(bare)) return true;
  if (word !== bare && bare.startsWith("/")) return true;
  if (word.startsWith("/")) {
    // `/help` or `/api` are not worth a round trip: want a second slash or a dot.
    const rest = word.slice(1);
    return rest.includes("/") || rest.includes(".");
  }
  return false;
}

const IMAGE_SUFFIX = /\.(png|jpe?g|gif|webp|bmp|tiff?|heic)$/i;

/** The path a pasted word names: quotes and `file://` removed, percent-escapes decoded. */
export function pastedPath(text: string): string {
  let word = text.trim();
  if (word.length >= 2 && (word[0] === '"' || word[0] === "'") && word.at(-1) === word[0]) {
    word = word.slice(1, -1);
  }
  if (word.startsWith("file://")) {
    word = word.slice("file://".length);
    try {
      word = decodeURIComponent(word);
    } catch {
      // keep the raw form; the bridge will refuse it if it is wrong
    }
  }
  return word;
}

export function isImagePath(text: string): boolean {
  return IMAGE_SUFFIX.test(pastedPath(text));
}

export function lineCount(text: string): number {
  return text.split("\n").length;
}

/** Either limit reached collapses; a limit of 0 or less is off. */
export function shouldCollapse(text: string, maxLines: number, maxChars: number): boolean {
  return (maxLines > 0 && lineCount(text) >= maxLines) || (maxChars > 0 && text.length >= maxChars);
}

export function pasteLabel(number: number, lines: number): string {
  return `[[paste ${number} · ${lines} ${lines === 1 ? "line" : "lines"}]]`;
}

export function imageLabel(number: number): string {
  return `[[image ${number}]]`;
}

/** The text the model receives: paste labels become their text, image labels disappear. */
export function expandTokens(text: string, tokens: readonly ComposerToken[]): string {
  let out = text;
  for (const token of tokens) {
    if (token.kind === "paste") out = out.split(token.label).join(token.text);
    else out = out.split(` ${token.label}`).join("").split(token.label).join("");
  }
  return out;
}

/** Only paste labels expanded; image labels stay (a slash argument keeps naming them). */
export function expandPastes(text: string, tokens: readonly ComposerToken[]): string {
  let out = text;
  for (const token of tokens)
    if (token.kind === "paste") out = out.split(token.label).join(token.text);
  return out;
}

export function hasImage(text: string, tokens: readonly ComposerToken[]): boolean {
  return tokens.some((token) => token.kind === "image" && text.includes(token.label));
}

export interface PasteDeps {
  readonly rpc: Rpc;
  readonly composer: Composer;
  sessionId(): string | null;
  capture(): Destination;
  isCurrent(dest: Destination): boolean;
  /** Collapse limits from config (`pasteLines`, `pasteChars`). */
  limits(): { lines: number; chars: number };
}

export type PasteOutcome = "empty" | "inline" | "collapsed" | "image" | "drop" | "stale";

function quoteIfSpaced(path: string): string {
  return /\s/.test(path) ? `"${path}"` : path;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

async function attachImage(deps: PasteDeps, sid: string, path: string): Promise<ImageToken | null> {
  const reply = asRecord(await deps.rpc.call("image.attach", { session_id: sid, path }));
  const resolved = typeof reply.path === "string" ? reply.path : path;
  const name = typeof reply.name === "string" ? reply.name : resolved;
  return {
    kind: "image",
    label: imageLabel(deps.composer.nextImageNumber()),
    path: resolved,
    name,
  };
}

/** Puts a paste into the composer; resolves once every bridge round trip has settled. */
export async function handlePaste(raw: string, deps: PasteDeps): Promise<PasteOutcome> {
  const text = cleanPaste(raw);
  if (!/[^\n]/.test(text)) return "empty";
  const { composer } = deps;
  const dest = deps.capture();
  const revision = composer.revision;
  const sid = deps.sessionId();
  // `moved`: the session changed — drop. `typed`: only the composer changed — go plain.
  const moved = (): boolean => !deps.isCurrent(dest);
  const typed = (): boolean => composer.revision !== revision;

  if (sid !== null && looksLikePath(text)) {
    if (isImagePath(text)) {
      try {
        const token = await attachImage(deps, sid, pastedPath(text));
        if (moved()) return "stale";
        if (!typed() && token !== null) {
          composer.addToken(token);
          return "image";
        }
      } catch {
        // not an image after all (or unreadable): fall through to drop detection
      }
    }
    if (moved()) return "stale";
    if (!typed()) {
      try {
        const drop = asRecord(await deps.rpc.call("input.detect_drop", { text }));
        if (moved()) return "stale";
        if (!typed() && drop.is_drop === true) {
          const paths = Array.isArray(drop.paths) ? drop.paths.map(String) : [];
          const images = new Set(Array.isArray(drop.images) ? drop.images.map(String) : []);
          const tokens: ImageToken[] = [];
          for (const path of paths.filter((item) => images.has(item))) {
            try {
              const token = await attachImage(deps, sid, path);
              if (token !== null) tokens.push(token);
            } catch {
              images.delete(path);
            }
          }
          if (moved()) return "stale";
          for (const token of tokens) composer.addToken(token);
          const rest = paths.filter((path) => !images.has(path)).map(quoteIfSpaced);
          if (rest.length > 0) {
            composer.insertLabel(rest.join(" "));
            composer.notify();
          }
          return "drop";
        }
      } catch {
        // drop detection unavailable: plain text below
      }
    }
    if (moved()) return "stale";
  }

  const { lines, chars } = deps.limits();
  if (shouldCollapse(text, lines, chars)) {
    const token: PasteToken = {
      kind: "paste",
      label: pasteLabel(composer.nextPasteNumber(), lineCount(text)),
      text,
    };
    composer.addToken(token);
    deps.rpc.call("paste.collapse", { text }).then(
      (reply) => {
        const path = asRecord(reply).path;
        if (typeof path === "string" && !moved()) token.path = path;
      },
      () => {
        // the token still holds the full text; only the file copy is missing
      },
    );
    return "collapsed";
  }
  composer.insertText(text);
  return "inline";
}

/**
 * Ctrl+V / Alt+V: clipboard text through the same paste path. `read` is `readClipboard` bound to
 * the terminal; a session switch while it reads drops the result.
 */
export async function pasteFromClipboard(
  deps: PasteDeps,
  read: () => Promise<string | null>,
  notice: (text: string) => void,
): Promise<PasteOutcome> {
  const dest = deps.capture();
  const text = await read();
  if (!deps.isCurrent(dest)) return "stale";
  if (text === null) {
    notice("the clipboard holds no text");
    return "empty";
  }
  return handlePaste(text, deps);
}
