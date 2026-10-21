import { describe, expect, it } from "vitest";
import {
  cleanHeaders,
  decodeHeaders,
  encodeHeaders,
  isDroppedHeader,
  matchesGateway,
  proxyHeadersFor,
} from "./proxyHeaders.ts";

const SECRET = ["access", "client", "value"].join("-");

describe("proxy headers", () => {
  it("Cookie in a configured header set is dropped", () => {
    const { headers, dropped } = cleanHeaders({
      "CF-Access-Client-Id": "id-1",
      "CF-Access-Client-Secret": SECRET,
      Cookie: "session=1",
      authorization: "Bearer x",
      HOST: "evil",
      Origin: "x",
      Referer: "x",
      "X-Indus-Session-Token": "x",
      "Sec-WebSocket-Protocol": "x",
      "Proxy-Authorization": "x",
      "Content-Length": "9",
      "Bad Name": "x",
    });
    expect(headers).toEqual({ "CF-Access-Client-Id": "id-1", "CF-Access-Client-Secret": SECRET });
    expect(dropped).toEqual([
      "Cookie",
      "authorization",
      "HOST",
      "Origin",
      "Referer",
      "X-Indus-Session-Token",
      "Sec-WebSocket-Protocol",
      "Proxy-Authorization",
      "Content-Length",
      "Bad Name",
    ]);
    expect(isDroppedHeader("COOKIE")).toBe(true);
  });

  it("values lose CR and LF and empty ones are refused", () => {
    expect(cleanHeaders({ A: "one\r\nInjected: yes", B: "  ", C: 3 })).toEqual({
      headers: { A: "oneInjected: yes" },
      dropped: ["B", "C"],
    });
  });

  it("headers go only to the connection's own gateway", () => {
    const base = "https://gw.example/indus";
    expect(matchesGateway("https://gw.example/indus/api/config", base)).toBe(true);
    expect(matchesGateway("wss://gw.example/indus/api/ws", base)).toBe(true);
    expect(matchesGateway("https://gw.example/indus", base)).toBe(true);
    expect(matchesGateway("https://gw.example/indusx/api", base)).toBe(false);
    expect(matchesGateway("http://gw.example/indus/api", base)).toBe(false);
    expect(matchesGateway("https://gw.example:8443/indus/api", base)).toBe(false);
    expect(matchesGateway("https://other.example/indus/api", base)).toBe(false);
    expect(matchesGateway("not a url", base)).toBe(false);
    expect(proxyHeadersFor("https://other.example/x", base, { A: "b" })).toEqual({});
    expect(proxyHeadersFor("https://gw.example/indus/api/x", base, { A: "b" })).toEqual({ A: "b" });
  });

  it("round-trips the stored form", () => {
    const { stored, dropped } = encodeHeaders({ A: "b", Cookie: "c" });
    expect(dropped).toEqual(["Cookie"]);
    expect(decodeHeaders(stored)).toEqual({ A: "b" });
    expect(decodeHeaders("{broken")).toEqual({});
    expect(encodeHeaders({}).stored).toBe("");
  });
});
