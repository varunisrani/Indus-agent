import { describe, expect, it, vi } from "vitest";
import { type AppOrigin, fileAppOrigin } from "../appOrigin.ts";
import {
  buildCsp,
  CspOriginError,
  CspState,
  cspOrigin,
  injectCspMeta,
  installCspHeaders,
  withCspHeader,
} from "./csp.ts";

const FILE = fileAppOrigin("/app/dist/index.html", "linux");
const DEV: AppOrigin = { kind: "dev", origin: "http://localhost:5173" };

describe("CSP", () => {
  it("packaged policy (snapshot)", () => {
    expect(
      buildCsp({ dev: false, connectOrigins: ["ws://127.0.0.1:41000", "http://127.0.0.1:9119"] }),
    ).toBe(
      "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
        "img-src 'self' data: blob: indus-media:; media-src 'self' blob: indus-media:; font-src 'self' data:; " +
        "connect-src 'self' ws://127.0.0.1:41000 http://127.0.0.1:9119; frame-src 'none'; object-src 'none'; " +
        "base-uri 'none'; form-action 'none'",
    );
  });

  it("dev policy adds only the dev server (scripts and HMR socket)", () => {
    const csp = buildCsp({ dev: true, devServerOrigin: "http://localhost:5173" });
    expect(csp).toContain("script-src 'self' http://localhost:5173;");
    expect(csp).toContain("connect-src 'self' http://localhost:5173 ws://localhost:5173;");
  });

  it("never allows eval or inline script", () => {
    for (const csp of [
      buildCsp({ dev: false }),
      buildCsp({ dev: true, devServerOrigin: "http://localhost:5173" }),
    ]) {
      const script = csp.split("; ").find((d) => d.startsWith("script-src")) ?? "";
      expect(script).not.toMatch(/unsafe-eval|unsafe-inline|\*|data:/);
    }
  });

  it("a dev origin is ignored in a packaged build", () => {
    expect(buildCsp({ dev: false, devServerOrigin: "http://localhost:5173" })).toBe(
      buildCsp({ dev: false }),
    );
  });

  it.each([
    "http://127.0.0.1:9/api",
    "https://x?y=1",
    "ws://u:p@h:1",
    "ftp://h",
    "http://*.x",
    "nope",
  ])("origin %s is refused", (raw) => {
    expect(() => cspOrigin(raw)).toThrow(CspOriginError);
  });

  it("CspState grows connect-src, de-duplicated", () => {
    const state = new CspState({ dev: false });
    state.addConnectOrigin("ws://127.0.0.1:41000");
    state.addConnectOrigin("ws://127.0.0.1:41000/");
    expect(state.current().match(/ws:\/\/127\.0\.0\.1:41000/g)).toHaveLength(1);
  });

  it("meta injection: one tag, first in head", () => {
    const html =
      '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="x"><title>t</title></head></html>';
    const out = injectCspMeta(html, "default-src 'self'");
    expect(out.match(/Content-Security-Policy/g)).toHaveLength(1);
    expect(out).toContain(
      `<head>\n    <meta http-equiv="Content-Security-Policy" content="default-src 'self'">`,
    );
    expect(() => injectCspMeta("<html></html>", "x")).toThrow();
  });

  it("header: replaces any CSP on app URLs, leaves other origins alone", () => {
    const headers = withCspHeader(
      {
        url: "http://localhost:5173/",
        responseHeaders: { "content-security-policy": ["unsafe"], "X-Other": ["1"] },
      },
      "default-src 'self'",
      DEV,
    );
    expect(headers).toEqual({
      "X-Other": ["1"],
      "Content-Security-Policy": ["default-src 'self'"],
    });
    const other = { url: "https://example.com/", responseHeaders: { a: ["b"] } };
    expect(withCspHeader(other, "x", DEV)).toBe(other.responseHeaders);
    expect(withCspHeader({ url: "file:///app/dist/index.html" }, "p", FILE)).toEqual({
      "Content-Security-Policy": ["p"],
    });
  });

  it("installs a header listener filtered to app URLs", () => {
    const onHeadersReceived = vi.fn();
    installCspHeaders({ webRequest: { onHeadersReceived } }, DEV, () => "p");
    expect(onHeadersReceived.mock.calls[0]?.[0]).toEqual({ urls: ["http://localhost:5173/*"] });
    const callback = vi.fn();
    onHeadersReceived.mock.calls[0]?.[1]({ url: "http://localhost:5173/x" }, callback);
    expect(callback).toHaveBeenCalledWith({
      responseHeaders: { "Content-Security-Policy": ["p"] },
    });
  });
});
