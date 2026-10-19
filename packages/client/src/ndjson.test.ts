import { describe, expect, it } from "vitest";
import { encodeFrame, LineSplitter, parseFrameLine, preview } from "./ndjson.ts";

function collect(options = {}) {
  const lines: string[] = [];
  const splitter = new LineSplitter((line) => lines.push(line), options);
  return { lines, splitter };
}

describe("LineSplitter", () => {
  it("joins a frame cut across reads, including inside a multi-byte character", () => {
    const { lines, splitter } = collect();
    const bytes = new TextEncoder().encode('{"text":"naïve ☕"}\n{"b":2}\n');
    const cut = bytes.indexOf(0xe2) + 1;
    splitter.push(bytes.subarray(0, 4));
    splitter.push(bytes.subarray(4, cut));
    splitter.push(bytes.subarray(cut));
    expect(lines).toEqual(['{"text":"naïve ☕"}', '{"b":2}']);
  });

  it("strips CRLF, skips blank lines and flushes a last line at end of stream", () => {
    const { lines, splitter } = collect();
    splitter.push('{"a":1}\r\n\r\n   \n{"b":2}');
    expect(lines).toEqual(['{"a":1}']);
    splitter.end();
    expect(lines).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("passes a 1 MiB line through whole", () => {
    const { lines, splitter } = collect();
    const big = encodeFrame({
      jsonrpc: "2.0",
      method: "notice",
      params: { text: "y".repeat(1 << 20) },
    });
    for (let at = 0; at < big.length; at += 65_536) splitter.push(big.slice(at, at + 65_536));
    expect(lines).toHaveLength(1);
    expect(lines[0]?.length).toBe(big.length - 1);
  });

  it("drops a line over the cap and reports it, then carries on", () => {
    const oversize: number[] = [];
    const { lines, splitter } = collect({
      maxLineBytes: 100,
      onOversize: (n: number) => oversize.push(n),
    });
    splitter.push("z".repeat(60));
    splitter.push("z".repeat(60));
    splitter.push('z\n{"ok":true}\n');
    expect(oversize).toHaveLength(1);
    expect(lines).toEqual(['{"ok":true}']);
  });
});

describe("parseFrameLine", () => {
  it("accepts JSON objects only", () => {
    expect(parseFrameLine('{"jsonrpc":"2.0"}')).toEqual({ ok: true, frame: { jsonrpc: "2.0" } });
    expect(parseFrameLine("[1,2]").ok).toBe(false);
    expect(parseFrameLine("42").ok).toBe(false);
    expect(parseFrameLine("null").ok).toBe(false);
    expect(parseFrameLine("Traceback: boom")).toEqual({ ok: false, preview: "Traceback: boom" });
  });

  it("keeps previews to 120 characters on one line", () => {
    const shown = preview(`${"w".repeat(200)}\nsecond line`);
    expect(shown.length).toBe(120);
    expect(shown.endsWith("…")).toBe(true);
    expect(preview("a\tb\r\nc")).toBe("a b c");
  });

  it("encodes one frame per line", () => {
    expect(encodeFrame({ text: "two\nlines" })).toBe('{"text":"two\\nlines"}\n');
  });
});
