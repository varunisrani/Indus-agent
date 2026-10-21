import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ResolvedConfig } from "vite";
import { describe, expect, it } from "vitest";
import {
  cspMetaPlugin,
  DEV_ORIGIN,
  RUNTIME_CONNECT_SOURCES,
  rendererMetaCsp,
} from "../vite.config.ts";

const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function directive(policy: string, name: string): string[] {
  const found = policy.split("; ").find((part) => part.startsWith(`${name} `));
  return found === undefined ? [] : found.split(" ").slice(1);
}

function transform(command: "serve" | "build", html: string): string {
  const plugin = cspMetaPlugin();
  (plugin.configResolved as (config: ResolvedConfig) => void)({ command } as ResolvedConfig);
  const hook = plugin.transformIndexHtml as { handler: (html: string) => string };
  return hook.handler(html);
}

describe("renderer CSP meta", () => {
  it("allows no inline or eval'd script, in dev or build", () => {
    for (const policy of [
      rendererMetaCsp({ dev: false }),
      rendererMetaCsp({ dev: true, devServerOrigin: DEV_ORIGIN }),
    ]) {
      expect(policy).not.toContain("unsafe-eval");
      expect(directive(policy, "script-src")).not.toContain("'unsafe-inline'");
      expect(directive(policy, "object-src")).toEqual(["'none'"]);
      expect(directive(policy, "frame-src")).toEqual(["'none'"]);
      for (const source of RUNTIME_CONNECT_SOURCES) {
        expect(directive(policy, "connect-src")).toContain(source);
      }
    }
    expect(directive(rendererMetaCsp({ dev: false }), "script-src")).toEqual(["'self'"]);
    expect(
      directive(rendererMetaCsp({ dev: true, devServerOrigin: DEV_ORIGIN }), "script-src"),
    ).toEqual(["'self'", DEV_ORIGIN]);
  });

  it("is the first tag in <head> of the page, and the page has no inline script", () => {
    const html = fs.readFileSync(path.join(APP, "index.html"), "utf8");
    const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
    expect(scripts.length).toBeGreaterThan(0);
    for (const script of scripts) {
      expect(script[1]).toContain("src=");
      expect((script[2] ?? "").trim()).toBe("");
    }
    for (const command of ["serve", "build"] as const) {
      const out = transform(command, html);
      const head = out.slice(out.indexOf("<head>") + "<head>".length).trimStart();
      expect(head.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true);
      expect(out.match(/Content-Security-Policy/g)).toHaveLength(1);
    }
  });
});
