// P25-03 desktop security audit in a real Electron window (fake backend). The unit half, over the
// whole IPC table and every policy module, is `electron/security/audit.test.ts`; the hostile guest
// pages, fs confinement, deep links, media and diagnostics run in `core/security.electron.test.ts`.
// This spec adds what only a live window shows:
//
//   csp        the app page has exactly one policy meta, scripts limited to the app and checked
//              plugin modules, and (where the response has headers) main's header says the same;
//   bridge     only the app's main frame has `window.indusDesktop`: a same-origin iframe of the app
//              page has none, so a frame without the preload has no way to main at all;
//   payloads   every invoke method of the live bridge refuses an unknown key (`bad-request`) or is
//              not built yet (`not-available`) — no handler ever runs on a malformed payload;
//   navigation the app window cannot be navigated away, and window.open opens nothing;
//   fuses      read from the packaged binary when a package is there (CI's desktop-package job or
//              `packaged.electron.test.ts`): RunAsNode, NODE_OPTIONS and inspect args off, ASAR
//              integrity and only-load-from-ASAR on, cookie encryption on.

import fs from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { CHANNELS } from "../electron/ipc/channels.ts";
import { cleanupFakes } from "../testing/fakeIndus.ts";
import {
  APP_ROOT,
  type FakeBackend,
  fakeBackend,
  HEALTHY,
  launchApp,
  type RunningApp,
  skipReason,
  sleep,
  tempDir,
  waitConnected,
} from "./fixtures.ts";

const PACKAGED = path.join(APP_ROOT, "build", "release", "linux-unpacked");

let work = "";
let fake: FakeBackend | null = null;
let app: RunningApp | null = null;

beforeAll(async () => {
  if (skipReason !== "") return;
  work = tempDir("indus-e2e-audit-");
  fake = fakeBackend(work, HEALTHY);
  app = await launchApp({ work, home: fake.home, indus: fake.indus });
  await waitConnected(app.cdp);
}, 240_000);

afterAll(async () => {
  await app?.stop();
  cleanupFakes();
});

/** One extra DevTools session that records the main document's response headers on a reload. */
async function documentHeaders(running: RunningApp): Promise<Record<string, string> | null> {
  const page = (await running.targets()).find(
    (target) => target.type === "page" && target.url.startsWith("file:"),
  );
  if (page === undefined) return null;
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise<void>((resolve, reject) => {
    socket.onopen = () => resolve();
    socket.onerror = () => reject(new Error("devtools socket failed"));
  });
  try {
    return await new Promise<Record<string, string> | null>((resolve) => {
      const timer = setTimeout(() => resolve(null), 20_000);
      socket.onmessage = (message) => {
        const frame = JSON.parse(String(message.data)) as {
          method?: string;
          params?: { type?: string; response?: { headers?: Record<string, string> } };
        };
        if (frame.method === "Network.responseReceived" && frame.params?.type === "Document") {
          clearTimeout(timer);
          resolve(frame.params.response?.headers ?? {});
        }
      };
      socket.send(JSON.stringify({ id: 1, method: "Network.enable", params: {} }));
      socket.send(JSON.stringify({ id: 2, method: "Page.reload", params: {} }));
    });
  } finally {
    socket.close();
  }
}

describe.skipIf(skipReason !== "")(
  `desktop security audit${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it("the app page carries exactly one locked CSP meta", async () => {
      const running = app as RunningApp;
      const metas = await running.cdp.eval<string[]>(
        `[...document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]')].map((m) => m.content)`,
      );
      expect(metas.length).toBe(1);
      const policy = metas[0] ?? "";
      expect(policy).toMatch(/(^|; )script-src 'self' indus-plugin:(;|$)/);
      expect(policy).toMatch(/(^|; )object-src 'none'(;|$)/);
      expect(policy).toMatch(/(^|; )base-uri 'none'(;|$)/);
      expect(policy).not.toMatch(/unsafe-eval/);
    });

    it("only the app's main frame has the bridge", async () => {
      const running = app as RunningApp;
      const seen = await running.cdp.eval<{
        top: string;
        frame: string;
      }>(`new Promise((resolve) => {
        const frame = document.createElement("iframe");
        frame.style.display = "none";
        document.body.append(frame);
        const inner = frame.contentWindow;
        const answer = { top: typeof window.indusDesktop, frame: inner ? typeof inner.indusDesktop : "no window" };
        frame.remove();
        resolve(answer);
      })`);
      expect(seen).toEqual({ top: "object", frame: "undefined" });
    });

    it("every invoke method of the live bridge refuses a payload with an unknown key", async () => {
      const running = app as RunningApp;
      const calls = CHANNELS.filter((c) => c.kind === "invoke" && c.method !== null).map((c) => ({
        name: c.name,
        group: c.group,
        method: c.method as string,
      }));
      const answers = await running.cdp.eval<
        Array<{ name: string; outcome: string }>
      >(`(async () => {
        const calls = ${JSON.stringify(calls)};
        const out = [];
        for (const call of calls) {
          const fn = window.indusDesktop?.[call.group]?.[call.method];
          if (typeof fn !== "function") { out.push({ name: call.name, outcome: "missing" }); continue; }
          try {
            await fn({ __indus_audit_unknown_key__: 1 });
            out.push({ name: call.name, outcome: "resolved" });
          } catch (error) {
            out.push({ name: call.name, outcome: String(error && error.message || error).split(":")[0] });
          }
        }
        return out;
      })()`);
      expect(answers.length).toBe(calls.length);
      const wrong = answers.filter(
        (a) => a.outcome !== "bad-request" && a.outcome !== "not-available",
      );
      expect(wrong).toEqual([]);
    });

    it("the app window cannot be navigated away and window.open opens nothing", async () => {
      const running = app as RunningApp;
      const before = (await running.targets()).filter((t) => t.type === "page").length;
      const opened = await running.cdp.eval<string>(
        `window.open("https://example.invalid/popup") === null ? "null" : "window"`,
      );
      expect(opened).toBe("null");
      await running.cdp.eval(`(() => { location.href = "https://example.invalid/away"; })()`);
      await sleep(1_500);
      expect(await running.cdp.eval<string>(`location.protocol`)).toBe("file:");
      expect((await running.targets()).filter((t) => t.type === "page").length).toBe(before);
      expect(running.recorder.opened()).toEqual([]);
    });

    it("main's CSP header on the app page (where file responses carry headers)", async () => {
      const running = app as RunningApp;
      const headers = await documentHeaders(running);
      expect(headers).not.toBeNull();
      const lower = Object.fromEntries(
        Object.entries(headers ?? {}).map(([key, value]) => [key.toLowerCase(), value]),
      );
      if (Object.keys(lower).length > 0) {
        expect(lower["content-security-policy"] ?? "").toMatch(/script-src 'self' indus-plugin:/);
      }
      await waitConnected(running.cdp);
    });
  },
);

const packagedSkip =
  skipReason !== ""
    ? skipReason
    : fs.existsSync(PACKAGED)
      ? ""
      : "no packaged build here (CI's desktop-package job makes one)";

describe.skipIf(packagedSkip !== "")(
  `fuses of the packaged binary${packagedSkip ? ` (skipped: ${packagedSkip})` : ""}`,
  () => {
    it("RunAsNode, NODE_OPTIONS and inspect off; ASAR integrity, ASAR-only and cookie encryption on", async () => {
      // @ts-expect-error -- plain JS module without types
      const fuses = (await import("../scripts/verify-fuses.mjs")) as {
        binariesFor(args: string[]): string[];
        verify(binaries: string[]): Promise<{ ok: boolean; lines: string[] }>;
      };
      const binaries = fuses.binariesFor([PACKAGED]);
      expect(binaries.length).toBeGreaterThan(0);
      const result = await fuses.verify(binaries);
      expect(result.ok, result.lines.join("\n")).toBe(true);
    });
  },
);
