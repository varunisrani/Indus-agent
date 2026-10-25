import { describe, expect, it } from "vitest";
import { formatShortDay, formatTokens } from "./format.ts";

describe("formatTokens", () => {
  it("keeps small figures and shortens large ones", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(316)).toBe("316");
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(1_000)).toBe("1.0K");
    expect(formatTokens(3_712)).toBe("3.7K");
    expect(formatTokens(1_250_000)).toBe("1.3M");
  });
});

describe("formatShortDay", () => {
  it("names the month and day", () => {
    expect(formatShortDay("2026-10-24")).toBe("Oct 24");
    expect(formatShortDay("2026-01-05")).toBe("Jan 5");
  });

  it("leaves other text alone", () => {
    expect(formatShortDay("yesterday")).toBe("yesterday");
    expect(formatShortDay("2026-13-01")).toBe("2026-13-01");
  });
});
