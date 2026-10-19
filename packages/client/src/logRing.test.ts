import { describe, expect, it } from "vitest";
import { LOG_LINE_MAX_BYTES } from "./limits.ts";
import { cutLine, LogRing, redactUrls } from "./logRing.ts";

const bytes = (text: string) => new TextEncoder().encode(text).length;

describe("LogRing", () => {
  it("keeps the last 200 lines", () => {
    const ring = new LogRing();
    for (let i = 0; i < 250; i += 1) ring.push(`line ${i}`);
    expect(ring.size).toBe(200);
    expect(ring.lines()[0]).toBe("line 50");
    expect(ring.tail(2)).toEqual(["line 248", "line 249"]);
  });

  it("cuts a 5,000-byte line to 4,096 bytes with a visible marker", () => {
    const ring = new LogRing();
    ring.push("a".repeat(5_000));
    const [stored] = ring.lines();
    expect(bytes(stored as string)).toBeLessThanOrEqual(LOG_LINE_MAX_BYTES);
    expect(stored).toMatch(/…\[cut \d+ bytes\]$/);
    const kept = (stored as string).replace(/ …\[cut \d+ bytes\]$/, "").length;
    expect(stored).toContain(`[cut ${5_000 - kept} bytes]`);
  });

  it("never splits a multi-byte character", () => {
    const line = "é".repeat(3_000);
    const cut = cutLine(line, 101);
    expect(bytes(cut)).toBeLessThanOrEqual(101);
    expect(cut).not.toContain("�");
    expect(cutLine("short")).toBe("short");
  });

  it("redacts query strings, fragments and user-info before storing", () => {
    const ring = new LogRing();
    ring.push("connecting to wss://host.example:9000/ws?token=x&b=2 now");
    ring.push("fetch https://user:pw@internal.example/a/b#frag done");
    ring.push("plain text, no url");
    expect(ring.lines()).toEqual([
      "connecting to wss://host.example:9000/ws?*** now",
      "fetch https://***@internal.example/a/b#*** done",
      "plain text, no url",
    ]);
  });

  it("redacts before cutting, so a cut can never expose a token", () => {
    const secret = `https://h.example/p?token=${"z".repeat(5_000)}`;
    const ring = new LogRing();
    ring.push(secret);
    expect(ring.lines()[0]).toBe("https://h.example/p?***");
    expect(redactUrls("ssh://git@host/repo")).toBe("ssh://***@host/repo");
  });

  it("splits multi-line text and skips empty lines", () => {
    const ring = new LogRing();
    ring.push("one\r\ntwo\n\nthree\n");
    expect(ring.lines()).toEqual(["one", "two", "three"]);
  });
});
