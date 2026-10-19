// The last diagnostic lines (child stderr, lifecycle, protocol noise) for `/logs` and timeouts.

import { LOG_LINE_MAX_BYTES, LOG_RING_LINES } from "./limits.ts";
import { Ring } from "./ring.ts";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

// scheme://anything-up-to-whitespace-or-quote
const URL_PATTERN = /\b[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^\s"'<>`]+/g;

/** Strip user-info, query string and fragment from one URL; tokens live there. */
function redactOne(url: string): string {
  const schemeEnd = url.indexOf("://") + 3;
  const scheme = url.slice(0, schemeEnd);
  let rest = url.slice(schemeEnd);
  const cut = rest.search(/[?#]/);
  const tail = cut >= 0 ? rest.slice(cut) : "";
  rest = cut >= 0 ? rest.slice(0, cut) : rest;
  const slash = rest.indexOf("/");
  const authority = slash >= 0 ? rest.slice(0, slash) : rest;
  const path = slash >= 0 ? rest.slice(slash) : "";
  const at = authority.lastIndexOf("@");
  const host = at >= 0 ? `***@${authority.slice(at + 1)}` : authority;
  let hidden = "";
  if (tail.startsWith("?")) hidden += "?***";
  if (tail.includes("#")) hidden += "#***";
  return `${scheme}${host}${path}${hidden}`;
}

/** Redact every URL found in a log line. */
export function redactUrls(text: string): string {
  return text.replace(URL_PATTERN, redactOne);
}

/**
 * Cut a line to at most `maxBytes` UTF-8 bytes, marker included. The marker says how many bytes
 * were cut. A multi-byte character is never split.
 */
export function cutLine(text: string, maxBytes: number = LOG_LINE_MAX_BYTES): string {
  const bytes = encoder.encode(text);
  if (bytes.length <= maxBytes) return text;
  const marker = (cut: number): string => ` …[cut ${cut} bytes]`;
  // Reserve room for the widest marker this line can need, then back up to a character start.
  let end = Math.max(0, maxBytes - encoder.encode(marker(bytes.length)).length);
  while (end > 0 && ((bytes[end] ?? 0) & 0xc0) === 0x80) end -= 1;
  return decoder.decode(bytes.subarray(0, end)) + marker(bytes.length - end);
}

export class LogRing {
  private readonly ring: Ring<string>;
  private readonly maxBytes: number;

  constructor(lines: number = LOG_RING_LINES, maxBytes: number = LOG_LINE_MAX_BYTES) {
    this.ring = new Ring<string>(lines);
    this.maxBytes = maxBytes;
  }

  /** Store one line: URLs redacted first, then cut. Multi-line text is split into lines. */
  push(text: string): void {
    for (const line of text.split(/\r?\n/)) {
      if (line === "") continue;
      this.ring.push(cutLine(redactUrls(line), this.maxBytes));
    }
  }

  lines(): string[] {
    return this.ring.toArray();
  }

  /** The newest `count` lines, oldest first. */
  tail(count: number): string[] {
    return this.ring.last(count);
  }

  get size(): number {
    return this.ring.size;
  }
}
