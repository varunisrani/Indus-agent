import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  IMAGE_DRAFTS_KEY,
  InflightJournal,
  type JournalMessage,
  MAX_AGE_MS,
  MAX_ATTACHMENT_REFS,
  MAX_ENTRY_CHARS,
  MAX_MESSAGES,
  MAX_TEXT_PART_CHARS,
  STORE_KEY,
  type StorageLike,
  shapeEntry,
  WRITE_THROTTLE_MS,
} from "./inflightJournal.ts";

class MemoryStorage implements StorageLike {
  readonly data = new Map<string, string>();
  writes = 0;
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.writes += 1;
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

const KIB = 1024;

function message(id: string, text: string, extra: Partial<JournalMessage> = {}): JournalMessage {
  return { id, role: "assistant", parts: [{ type: "text", text }], ...extra };
}

const snapshot = (messages: JournalMessage[]) => ({ turnId: "t1", startedAt: 1, messages });

describe("in-flight turn journal", () => {
  let now = 1_000;
  let storage: MemoryStorage;
  let journal: InflightJournal;

  beforeEach(() => {
    vi.useFakeTimers();
    now = 1_000;
    storage = new MemoryStorage();
    journal = new InflightJournal({ storage, now: () => now });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("throttles writes to one per 400 ms (trailing edge)", () => {
    journal.record("s1", snapshot([message("m1", "a")]));
    journal.record("s1", snapshot([message("m1", "ab")]));
    journal.record("s1", snapshot([message("m1", "abc")]));
    expect(storage.writes).toBe(0);
    vi.advanceTimersByTime(WRITE_THROTTLE_MS - 1);
    expect(storage.writes).toBe(0);
    vi.advanceTimersByTime(1);
    expect(storage.writes).toBe(1);
    expect(journal.read("s1")?.messages[0]?.parts[0]?.text).toBe("abc");
    journal.record("s1", snapshot([message("m1", "abcd")]));
    vi.advanceTimersByTime(WRITE_THROTTLE_MS);
    expect(storage.writes).toBe(2);
  });

  it("rejects a 161 KiB entry and keeps the previous one", () => {
    journal.record("s1", snapshot([message("m1", "small")]));
    journal.flush();
    // 161 KiB of text spread over parts each under the part cap
    const parts = Array.from({ length: 3 }, () => ({
      type: "text" as const,
      text: "y".repeat(Math.ceil((161 * KIB) / 3)),
    }));
    const big: JournalMessage = { id: "m2", role: "assistant", parts };
    expect(shapeEntry("s1", snapshot([big]), now)).toEqual({ ok: false, reason: "too-large" });
    journal.record("s1", snapshot([big]));
    journal.flush();
    expect(journal.read("s1")?.messages.map((m) => m.id)).toEqual(["m1"]);
    const fits = shapeEntry("s1", snapshot([message("m3", "z".repeat(100 * KIB))]), now);
    expect(fits.ok && fits.chars <= MAX_ENTRY_CHARS).toBe(true);
  });

  it("prunes an 8-day-old entry", () => {
    journal.record("old", snapshot([message("m1", "a")]));
    journal.flush();
    now += 8 * 24 * 60 * 60 * 1000;
    expect(journal.read("old")).toBeNull();
    expect(journal.prune()).toBe(1);
    expect(storage.getItem(STORE_KEY)).toBeNull();
  });

  it("keeps an entry younger than 7 days", () => {
    journal.record("s1", snapshot([message("m1", "a")]));
    journal.flush();
    now += MAX_AGE_MS - 1;
    expect(journal.prune()).toBe(0);
    expect(journal.read("s1")).not.toBeNull();
  });

  it("caps messages, text parts, metadata and attachment refs", () => {
    const many = Array.from({ length: MAX_MESSAGES + 6 }, (_, i) => message(`m${i}`, "t"));
    const shaped = shapeEntry("s1", snapshot(many), now);
    expect(shaped.ok && shaped.entry.messages.length).toBe(MAX_MESSAGES);
    expect(shaped.ok && shaped.entry.messages[0]?.id).toBe("m6");

    const long = shapeEntry(
      "s1",
      snapshot([message("m", "q".repeat(MAX_TEXT_PART_CHARS + 10))]),
      now,
    );
    expect(long.ok && long.entry.messages[0]?.parts[0]).toMatchObject({ clipped: true });
    expect(long.ok && long.entry.messages[0]?.parts[0]?.text.length).toBe(MAX_TEXT_PART_CHARS);

    const meta = shapeEntry(
      "s1",
      snapshot([
        message("a", "t", { metadata: { tool: "terminal" } }),
        message("b", "t", { metadata: { blob: "m".repeat(3 * KIB) } }),
      ]),
      now,
    );
    expect(meta.ok && meta.entry.messages[0]?.metadata).toEqual({ tool: "terminal" });
    expect(meta.ok && meta.entry.messages[1]?.metadata).toBeUndefined();

    const refs = Array.from({ length: MAX_ATTACHMENT_REFS + 4 }, (_, i) => `ref-${i}`);
    const withRefs = shapeEntry("s1", snapshot([message("u", "t", { attachments: refs })]), now);
    expect(withRefs.ok && withRefs.entry.messages[0]?.attachments?.length).toBe(
      MAX_ATTACHMENT_REFS,
    );
  });

  it("evicts the oldest sessions to stay within 4 MiB", () => {
    const chunk = "w".repeat(60 * KIB);
    const twoParts = (id: string): JournalMessage => ({
      id,
      role: "assistant",
      parts: [
        { type: "text", text: chunk },
        { type: "text", text: chunk },
      ],
    });
    for (let i = 0; i < 40; i += 1) {
      now += 1;
      journal.record(`s${i}`, snapshot([twoParts(`m${i}`)]));
      journal.flush();
    }
    const raw = storage.getItem(STORE_KEY) ?? "";
    expect(raw.length).toBeLessThanOrEqual(4 * 1024 * 1024);
    expect(journal.read("s0")).toBeNull();
    expect(journal.read("s39")).not.toBeNull();
  });

  it("clear removes a session's entry and its pending write", () => {
    journal.record("s1", snapshot([message("m1", "a")]));
    journal.clear("s1");
    vi.advanceTimersByTime(WRITE_THROTTLE_MS);
    expect(journal.read("s1")).toBeNull();
  });

  it("broken storage never throws", () => {
    const broken: StorageLike = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    const safe = new InflightJournal({ storage: broken });
    expect(() => {
      safe.record("s1", snapshot([message("m1", "a")]));
      safe.flush();
      safe.read("s1");
      safe.saveImageDraft("s1", { requestId: "r", text: "x", attachments: [] });
    }).not.toThrow();
  });

  it("an image draft survives a restart and is cleared by its acknowledgement only", () => {
    journal.saveImageDraft("s1", { requestId: "r1", text: "look", attachments: ["a.png"] });
    const reopened = new InflightJournal({ storage, now: () => now });
    expect(reopened.imageDraft("s1")).toMatchObject({ requestId: "r1", text: "look" });
    reopened.clearImageDraft("s1", "other");
    expect(reopened.imageDraft("s1")).not.toBeNull();
    reopened.clearImageDraft("s1", "r1");
    expect(reopened.imageDraft("s1")).toBeNull();
    expect(storage.getItem(IMAGE_DRAFTS_KEY)).toBeNull();
  });
});
