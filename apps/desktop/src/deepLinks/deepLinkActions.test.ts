import { describe, expect, it } from "vitest";
import { allowedServerUrl, planLink } from "./deepLinkActions.ts";
import { MEDIA_ORIGIN, mediaUrl } from "./mediaUrl.ts";

describe("deep link actions", () => {
  it("open goes only to an app route; settings links keep their place", () => {
    expect(planLink({ kind: "open", name: "abc-123", params: {} })).toEqual({
      kind: "navigate",
      path: "/abc-123",
      overlay: false,
    });
    expect(
      planLink({ kind: "open", name: "settings", params: { view: "safety", evil: "x" } }),
    ).toEqual({
      kind: "navigate",
      path: "/settings?view=safety",
      overlay: true,
    });
    for (const name of ["https://evil.example", "a/b", "../x", "x?y", "x#y"]) {
      expect(planLink({ kind: "open", name, params: {} }).kind).toBe("refused");
    }
  });

  it("skill and mcp wait for a confirmation", () => {
    expect(planLink({ kind: "skill", name: "team/review-helper", params: {} })).toEqual({
      kind: "confirm-skill",
      identifier: "team/review-helper",
    });
    expect(
      planLink({ kind: "mcp", name: "docs", params: { url: "https://mcp.example/sse" } }),
    ).toEqual({
      kind: "confirm-mcp",
      name: "docs",
      url: "https://mcp.example/sse",
      auth: "none",
    });
  });

  it("a link never adds a server that runs a program, or a plain-http remote", () => {
    expect(
      planLink({ kind: "mcp", name: "x", params: { command: "sh", url: "https://a.example" } })
        .kind,
    ).toBe("refused");
    expect(planLink({ kind: "mcp", name: "x", params: { url: "http://a.example" } }).kind).toBe(
      "refused",
    );
    expect(planLink({ kind: "mcp", name: "x y", params: { url: "https://a.example" } }).kind).toBe(
      "refused",
    );
    expect(planLink({ kind: "skill", name: "../../etc", params: {} }).kind).toBe("refused");
    expect(allowedServerUrl("http://127.0.0.1:9000/mcp")).toBe("http://127.0.0.1:9000/mcp");
    expect(allowedServerUrl("https://user:pw@a.example")).toBeNull();
    expect(allowedServerUrl("file:///etc/passwd")).toBeNull();
  });

  it("close-preview is main's business", () => {
    expect(planLink({ kind: "close-preview", name: "", params: {} })).toEqual({ kind: "ignore" });
  });

  it("media URLs encode the whole path under the stream host", () => {
    expect(mediaUrl("/home/me/a b.png")).toBe(`${MEDIA_ORIGIN}/%2Fhome%2Fme%2Fa%20b.png`);
  });
});
