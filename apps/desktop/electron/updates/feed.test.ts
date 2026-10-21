import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeFeed } from "../../testing/fakeFeed.ts";
import { FeedError, pickRelease, ReleaseFeed, tokenAllowed } from "./feed.ts";

const timers = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

// Fake credentials are built at run time (never a credential-shaped literal in the repo).
const fakeToken = (name: string) => ["fake", name, "value"].join("-");

describe("release selection", () => {
  const releases = [
    { tag_name: "v1.2.0", html_url: "https://example.invalid/r/1.2.0" },
    { tag_name: "v1.3.0-canary.20261001T000000Z" },
    { tag_name: "v1.3.0-canary.20261010T000000Z" },
    { tag_name: "v1.4.0", draft: true },
    { tag_name: "nightly-2026" },
    { tag_name: "v1.2.1" },
    { nope: true },
  ];

  it("stable takes the newest plain release; drafts and other tags are ignored", () => {
    expect(pickRelease(releases, "stable")).toMatchObject({ version: "1.2.1", canary: false });
  });

  it("canary takes the newest canary, and a newer stable over an older canary", () => {
    expect(pickRelease(releases, "canary")).toMatchObject({
      version: "1.3.0-canary.20261010T000000Z",
      canary: true,
    });
    expect(pickRelease([...releases, { tag_name: "v1.3.0" }], "canary")?.version).toBe("1.3.0");
  });

  it("an answer that is not a list is an error; an empty list is null", () => {
    expect(() => pickRelease({}, "stable")).toThrow(FeedError);
    expect(pickRelease([], "stable")).toBeNull();
  });

  it("a release page that is not https is not offered as a link", () => {
    expect(
      pickRelease([{ tag_name: "v2.0.0", html_url: "javascript:alert(1)" }], "stable")?.url,
    ).toBeNull();
  });

  it("tokens go only over https or to a loopback test feed", () => {
    expect(tokenAllowed("https://api.github.com/repos/x/y/releases")).toBe(true);
    expect(tokenAllowed("http://127.0.0.1:9/releases")).toBe(true);
    expect(tokenAllowed("http://example.com/releases")).toBe(false);
    expect(tokenAllowed("nonsense")).toBe(false);
  });
});

describe("release feed against a loopback fake", () => {
  let feed: FakeFeed;
  let logs: string[];
  let cliCalls: number;

  beforeEach(async () => {
    feed = new FakeFeed();
    feed.releases = [{ tag_name: "v0.9.0" }, { tag_name: "v1.0.0" }];
    await feed.start();
    logs = [];
    cliCalls = 0;
  });

  afterEach(() => feed.stop());

  function client(env: Record<string, string>, cli: string | null = null) {
    return new ReleaseFeed({
      apiUrl: feed.url,
      env,
      cliToken: async () => {
        cliCalls += 1;
        return cli;
      },
      fetch: (url, init) => fetch(url, init),
      timers,
      log: (line) => logs.push(line),
    });
  }

  it("uses the environment token first", async () => {
    const token = fakeToken("env");
    const result = await client({ GH_TOKEN: token }, fakeToken("cli")).latest("stable");
    expect(result?.version).toBe("1.0.0");
    expect(feed.requests[0]?.authorization).toBe(`Bearer ${token}`);
    expect(feed.requests[0]?.path).toContain("per_page=30");
    expect(cliCalls).toBe(0);
  });

  it("then the GitHub CLI's login, then nothing", async () => {
    const cli = fakeToken("cli");
    await client({}, cli).latest("stable");
    expect(feed.requests[0]?.authorization).toBe(`Bearer ${cli}`);
    await client({}, null).latest("stable");
    expect(feed.requests[1]?.authorization).toBeNull();
  });

  it("a rejected token is logged once by its source (never its value) and the check runs anonymously", async () => {
    const token = fakeToken("stale");
    feed.rejected.add(`Bearer ${token}`);
    const updates = client({ GITHUB_TOKEN: token });
    expect((await updates.latest("stable"))?.version).toBe("1.0.0");
    expect(feed.requests.map((r) => r.authorization)).toEqual([`Bearer ${token}`, null]);
    await updates.latest("stable");
    expect(feed.requests[2]?.authorization).toBeNull(); // not tried again
    expect(logs).toEqual([
      "[updates] the token from GITHUB_TOKEN was rejected; checking without it",
    ]);
    expect(logs.join("\n")).not.toContain(token);
  });

  it("a failing feed is a FeedError", async () => {
    feed.status = 503;
    await expect(client({}).latest("stable")).rejects.toThrow(/answered 503/);
    await feed.stop();
    await expect(client({}).latest("stable")).rejects.toThrow(FeedError);
  });
});
