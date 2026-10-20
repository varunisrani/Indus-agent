import { describe, expect, it } from "vitest";
import { ProviderMissing, providerFrom, resolveProvider, WaError } from "./provider.ts";
import { createWaSocket } from "./testing/fakeSocket.ts";

describe("loading a provider", () => {
  it("refuses an empty name with ProviderMissing", () => {
    expect(() => resolveProvider("")).toThrow(ProviderMissing);
    expect(() => resolveProvider("   ")).toThrow(/INDUS_WA_PROVIDER/);
  });

  it("imports an absolute path as a file URL and a package name as written", () => {
    expect(resolveProvider("/opt/wa/provider.mjs")).toBe("file:///opt/wa/provider.mjs");
    expect(resolveProvider(" some-provider ")).toBe("some-provider");
  });

  it("takes a module's own createWaSocket and refuses modules without one", () => {
    expect(providerFrom({ createWaSocket })).toBe(createWaSocket);
    expect(() => providerFrom({})).toThrow(/createWaSocket/);
    expect(() => providerFrom({ createWaSocket: "not a function" })).toThrow(/createWaSocket/);
  });
});

describe("errors", () => {
  it("carry our code and an optional wait", () => {
    const error = new WaError("rate_limited", "", 5);
    expect(error.code).toBe("rate_limited");
    expect(error.message).toBe("rate_limited");
    expect(error.retryAfter).toBe(5);
  });
});
