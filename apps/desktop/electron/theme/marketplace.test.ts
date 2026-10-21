import { describe, expect, it, vi } from "vitest";
import {
  allowedUrl,
  type Fetcher,
  type FetchResponse,
  MAX_VSIX_BYTES,
  MarketplaceClient,
  packageUrl,
  parseSearch,
  searchBody,
} from "./marketplace.ts";

function response(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): FetchResponse {
  return {
    status,
    headers: { get: (name) => headers[name.toLowerCase()] ?? null },
    arrayBuffer: async () => {
      const bytes =
        body instanceof Uint8Array ? body : new TextEncoder().encode(JSON.stringify(body));
      return bytes.slice().buffer as ArrayBuffer;
    },
    json: async () => body,
  };
}

const SEARCH_ANSWER = {
  results: [
    {
      extensions: [
        {
          publisher: { publisherName: "acme" },
          extensionName: "night-owl-ish",
          displayName: "Night Owlish",
          shortDescription: "A calm dark theme",
          versions: [{ version: "2.1.0" }],
          statistics: [{ statisticName: "install", value: 1234 }],
        },
        { extensionName: "broken" },
      ],
    },
  ],
};

describe("marketplace", () => {
  it("allows only https on the gallery hosts", () => {
    expect(allowedUrl("https://marketplace.visualstudio.com/_apis/x")).toBe(true);
    expect(allowedUrl("https://acme.gallerycdn.vsassets.io/x.vsix")).toBe(true);
    expect(allowedUrl("http://marketplace.visualstudio.com/")).toBe(false);
    expect(allowedUrl("https://marketplace.visualstudio.com.evil.example/")).toBe(false);
    expect(allowedUrl("https://user:pw@marketplace.visualstudio.com/")).toBe(false);
    expect(allowedUrl("not a url")).toBe(false);
  });

  it("an empty query lists the most installed themes; text narrows it", () => {
    const empty = JSON.parse(searchBody("  "));
    expect(empty.filters[0].criteria).toHaveLength(2);
    expect(empty.filters[0].sortBy).toBe(4);
    const text = JSON.parse(searchBody("owl"));
    expect(text.filters[0].criteria.at(-1)).toEqual({ filterType: 10, value: "owl" });
  });

  it("parses search answers and skips broken rows", () => {
    expect(parseSearch(SEARCH_ANSWER)).toEqual([
      {
        publisher: "acme",
        extension: "night-owl-ish",
        version: "2.1.0",
        displayName: "Night Owlish",
        description: "A calm dark theme",
        installs: 1234,
      },
    ]);
    expect(parseSearch(null)).toEqual([]);
  });

  it("downloads through allowed redirects, without credentials", async () => {
    const fetcher = vi.fn<Fetcher>(async (url) =>
      url.startsWith("https://marketplace")
        ? response(302, null, { location: "https://acme.gallerycdn.vsassets.io/pkg.vsix" })
        : response(200, new Uint8Array([80, 75, 3, 4])),
    );
    const client = new MarketplaceClient(fetcher);
    expect(await client.download("acme", "night-owl-ish", "2.1.0")).toBe(
      Buffer.from([80, 75, 3, 4]).toString("base64"),
    );
    expect(fetcher.mock.calls[0]?.[0]).toBe(packageUrl("acme", "night-owl-ish", "2.1.0"));
    expect(fetcher.mock.calls.every(([, init]) => init.credentials === "omit")).toBe(true);
    expect(fetcher.mock.calls.every(([, init]) => init.redirect === "manual")).toBe(true);
  });

  it("refuses a redirect off the allow-list, a too large package and errors", async () => {
    const offList = new MarketplaceClient(async () =>
      response(302, null, { location: "https://evil.example/pkg" }),
    );
    await expect(offList.download("a", "b", "1.0.0")).rejects.toThrow(/not allowed/);
    const big = new MarketplaceClient(async () =>
      response(200, new Uint8Array(1), { "content-length": String(MAX_VSIX_BYTES + 1) }),
    );
    await expect(big.download("a", "b", "1.0.0")).rejects.toThrow(/over 5 MiB/);
    const down = new MarketplaceClient(async () => response(503, null));
    await expect(down.search("x")).rejects.toThrow(/answered 503/);
    const loop = new MarketplaceClient(async () =>
      response(302, null, { location: "https://marketplace.visualstudio.com/again" }),
    );
    await expect(loop.search("x")).rejects.toThrow(/too many redirects/);
  });

  it("search posts the query", async () => {
    const fetcher = vi.fn<Fetcher>(async () => response(200, SEARCH_ANSWER));
    const items = await new MarketplaceClient(fetcher).search("owl");
    expect(items).toHaveLength(1);
    expect(fetcher.mock.calls[0]?.[1].method).toBe("POST");
    expect(fetcher.mock.calls[0]?.[1].body).toContain('"owl"');
  });
});
