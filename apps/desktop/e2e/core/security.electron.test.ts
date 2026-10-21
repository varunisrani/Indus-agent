// P20-40 security specs in the wired app (fake backend). The OS browser is a recorder on PATH,
// so "nothing opened" is observed where it would happen.
//
//   guests      the hostile pages (`testing/guestPages`) in the real preview pane: window.open,
//               untrusted clicks and form posts, file: / indus: / javascript: links, a sandboxed
//               iframe reaching for the bridge → nothing opens, no new window, no bridge; a
//               trusted click on an https `_blank` link opens exactly that link (the recorder
//               works), a trusted click on a file: link opens nothing;
//   fs          reads confined to the window's folders and the home, before and after symlinks;
//               key stores and `.env` files refused;
//   terminal    the shell's environment has no provider key the app was started with;
//   deep links  an MCP link that would run a program is refused; an https one and a skill link
//               only ever ask, and Cancel installs nothing;
//   media       `indus-media://` serves an image under the home, not one outside the roots;
//   links       link previews refuse private hosts (nothing reaches the loopback server);
//   diagnostics the copied bundle carries no secret the app was started with;
//   CSP         an inline script injected into the app page does not run.

import fs from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { cleanupFakes } from "../../testing/fakeIndus.ts";
import {
  bridge,
  clickAt,
  clickButton,
  DEV_SCHEME,
  type FakeBackend,
  fakeBackend,
  fillField,
  GUEST_PAGES,
  HEALTHY,
  hasButton,
  launchApp,
  type RunningApp,
  skipReason,
  sleep,
  tempDir,
  waitConnected,
} from "../fixtures.ts";

let work = "";
let fake: FakeBackend | null = null;
let app: RunningApp | null = null;
let site: http.Server | null = null;
let base = "";
const siteHits: string[] = [];
// fake secrets built at run time (never a credential-shaped literal in the source)
const PROVIDER_KEY = ["sk", "e2e", "0123456789abcdef0123456789abcdef"].join("-");
const PNG = Buffer.from(
  "89504e470d0a1a0a0000000d4948445200000001000000010806000000" +
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082",
  "hex",
);

const TRUSTED_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>trusted</title></head>
<body style="margin:0">
<a id="web" href="https://example.invalid/opened-by-a-person" target="_blank"
   style="display:block;width:300px;height:60px;background:#ddd">web link</a>
<a id="file" href="file:///etc/hostname" target="_blank"
   style="display:block;width:300px;height:60px;background:#eee">file link</a>
<script>window.__results = { done: true };</script></body></html>`;

async function previewOpen(running: RunningApp, url: string) {
  if (!(await running.cdp.eval<boolean>(`!!document.getElementById('pane-tab-preview')`))) {
    await clickButton(running.cdp, "Open Preview");
  }
  await clickAt(running.cdp, "#pane-tab-preview");
  await fillField(running.cdp, 'input[aria-label="Address"]', url);
  await running.cdp.eval(
    `document.querySelector('input[aria-label="Address"]').form.requestSubmit()`,
  );
  return await running.attach(
    (target) => target.type === "webview" && target.url === url,
    `the guest ${url}`,
  );
}

async function pageTargets(running: RunningApp): Promise<number> {
  return (await running.targets()).filter((t) => t.type === "page").length;
}

beforeAll(async () => {
  if (skipReason !== "") return;
  work = tempDir("indus-e2e-sec-");
  site = http.createServer((request, response) => {
    siteHits.push(request.url ?? "");
    const name = (request.url ?? "/").replace(/^\//, "").split("?")[0] ?? "";
    if (name === "trusted.html") {
      response.writeHead(200, { "content-type": "text/html" }).end(TRUSTED_PAGE);
      return;
    }
    const file = path.join(GUEST_PAGES, path.basename(name));
    if (name !== "" && fs.existsSync(file)) {
      response.writeHead(200, { "content-type": "text/html" }).end(fs.readFileSync(file));
      return;
    }
    response.writeHead(404).end();
  });
  await new Promise<void>((resolve) => site?.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(site.address() as AddressInfo).port}`;
  fake = fakeBackend(work, HEALTHY);
  const userHome = path.join(work, "user-home");
  fs.mkdirSync(userHome, { recursive: true });
  fs.writeFileSync(path.join(userHome, "inside.png"), PNG);
  fs.writeFileSync(path.join(work, "outside.png"), PNG);
  app = await launchApp({
    work,
    home: fake.home,
    indus: fake.indus,
    env: {
      HOME: userHome,
      OPENAI_API_KEY: PROVIDER_KEY,
      ANTHROPIC_API_KEY: PROVIDER_KEY,
    },
  });
  await waitConnected(app.cdp);
}, 240_000);

afterAll(async () => {
  await app?.stop();
  site?.close();
  cleanupFakes();
});

describe.skipIf(skipReason !== "")(
  `security${skipReason ? ` (skipped: ${skipReason})` : ""}`,
  () => {
    it.each([
      ["window-open.html"],
      ["synthetic-click.html"],
      ["file-link.html"],
      ["iframe-bridge.html"],
    ])(
      "hostile guest %s: nothing opens, no window, no bridge",
      async (page) => {
        const running = app as RunningApp;
        const before = await pageTargets(running);
        const guest = await previewOpen(running, `${base}/${page}`);
        await guest.waitFor(`window.__results?.done === true`, "the guest's own checks", 20_000);
        const results = await guest.eval<Record<string, unknown>>(`window.__results`);
        expect(await guest.eval<string>(`typeof window.indusDesktop`)).toBe("undefined");
        if (page === "iframe-bridge.html") {
          expect(results.frame).toMatchObject({ bridge: "blocked", ownBridge: "undefined" });
          expect((results.frame as { open: string }).open).not.toBe("window");
        }
        if (typeof results.href === "string") expect(results.href).toBe(`${base}/${page}`);
        guest.close();
        await sleep(500);
        expect(running.recorder.opened()).toEqual([]);
        expect(await pageTargets(running)).toBe(before);
      },
      60_000,
    );

    it("a trusted click opens exactly that https link; a trusted click on file: opens nothing", async () => {
      const running = app as RunningApp;
      const guest = await previewOpen(running, `${base}/trusted.html`);
      await guest.waitFor(`window.__results?.done === true`, "the trusted page");
      const clickIn = async (id: string) => {
        const at = await guest.eval<{ x: number; y: number }>(`(() => {
        const r = document.getElementById(${JSON.stringify(id)}).getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      })()`);
        for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
          await guest.send("Input.dispatchMouseEvent", {
            type,
            x: at.x,
            y: at.y,
            button: "left",
            clickCount: 1,
          });
        }
      };
      await clickIn("file");
      await sleep(1_000);
      expect(running.recorder.opened()).toEqual([]);
      await clickIn("web");
      const until = Date.now() + 10_000;
      while (running.recorder.opened().length === 0 && Date.now() < until) await sleep(100);
      expect(running.recorder.opened()).toEqual(["https://example.invalid/opened-by-a-person"]);
      guest.close();
    }, 60_000);

    it("fs reads stay inside the window's folders and the home, through symlinks too", async () => {
      const running = app as RunningApp;
      const folder = path.join(work, "project");
      fs.mkdirSync(folder, { recursive: true });
      fs.writeFileSync(path.join(folder, "ok.txt"), "fine\n");
      fs.writeFileSync(path.join(folder, ".env"), `OPENAI_API_KEY=${PROVIDER_KEY}\n`);
      fs.writeFileSync(path.join(folder, "id_ed25519"), "private\n");
      fs.symlinkSync("/etc/hostname", path.join(folder, "escape.txt"));
      const roots = await bridge<{ roots: string[] }>(
        running.cdp,
        `b.fs.setRoots({ roots: [${JSON.stringify(folder)}, "/etc"] })`,
      );
      expect(roots).toMatchObject({ ok: true, value: { roots: [folder] } });
      const read = (file: string) =>
        bridge<{ text: string }>(running.cdp, `b.fs.readText({ path: ${JSON.stringify(file)} })`);
      expect(await read(path.join(folder, "ok.txt"))).toMatchObject({
        ok: true,
        value: { text: "fine\n" },
      });
      for (const refused of [
        "/etc/passwd",
        `${folder}/../../../etc/passwd`,
        path.join(folder, "escape.txt"),
        path.join(folder, ".env"),
        path.join(folder, "id_ed25519"),
      ]) {
        const answer = await read(refused);
        expect(answer.ok, refused).toBe(false);
      }
    });

    it("the terminal's environment carries no provider key the app was started with", async () => {
      const running = app as RunningApp;
      await running.cdp.eval(`(() => {
      window.__term = "";
      window.indusDesktop.terminal.onData(({ data }) => { window.__term += data; });
    })()`);
      const started = await bridge<{ terminalId: string }>(
        running.cdp,
        `b.terminal.start({ cols: 120, rows: 30 })`,
      );
      if (!started.ok) throw new Error(started.error);
      const id = started.value.terminalId;
      await running.cdp.eval(
        `window.indusDesktop.terminal.write({ terminalId: ${JSON.stringify(id)}, data: "env; echo ENV-DONE\\r" })`,
      );
      await running.cdp.waitFor(
        `window.__term.includes("ENV-DONE\\r\\n")`,
        "the env listing",
        20_000,
      );
      const text = await running.cdp.eval<string>(`window.__term`);
      expect(text).toContain("PATH=");
      expect(text).not.toContain(PROVIDER_KEY);
      expect(text).not.toMatch(/OPENAI_API_KEY|ANTHROPIC_API_KEY|INDUS_DESKTOP_INDUS=/);
      await bridge(running.cdp, `b.terminal.dispose({ terminalId: ${JSON.stringify(id)} })`);
    });

    it("deep links: a program-running MCP link is refused; others only ask; Cancel installs nothing", async () => {
      const running = app as RunningApp;
      const restBefore = (await fake?.restCalls())?.length ?? 0;
      await running.secondLaunch(`${DEV_SCHEME}://mcp/evil?command=rm&args=-rf`);
      await running.cdp.waitFor(
        `document.body.innerText.includes('A link may not add a server that runs a program')`,
        "the refusal",
      );
      await running.secondLaunch(
        `${DEV_SCHEME}://mcp/docs?url=https%3A%2F%2Fexample.invalid%2Fmcp`,
      );
      await running.cdp.waitFor(hasButton("Add the server"), "the MCP confirmation");
      await clickButton(running.cdp, "Cancel");
      await running.secondLaunch(`${DEV_SCHEME}://skill/demo-skill`);
      await running.cdp.waitFor(hasButton("Install"), "the skill confirmation");
      await clickButton(running.cdp, "Cancel");
      await sleep(500);
      const calls = ((await fake?.restCalls()) ?? []).slice(restBefore);
      expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
      expect(running.recorder.opened()).toEqual(["https://example.invalid/opened-by-a-person"]);
    }, 90_000);

    it("media: an image under the home loads, one outside the roots does not", async () => {
      const running = app as RunningApp;
      const load = (file: string) =>
        running.cdp.eval<string>(`new Promise((resolve) => {
        const img = new Image();
        img.onload = () => resolve("loaded");
        img.onerror = () => resolve("refused");
        img.src = "indus-media://stream/" + encodeURIComponent(${JSON.stringify(file)});
      })`);
      expect(await load(path.join(work, "user-home", "inside.png"))).toBe("loaded");
      expect(await load(path.join(work, "outside.png"))).toBe("refused");
      expect(await load("/etc/hostname")).toBe("refused");
    });

    it("link previews refuse private hosts: the loopback server is never asked", async () => {
      const running = app as RunningApp;
      const hits = siteHits.length;
      const answer = await bridge<{ title: string | null }>(
        running.cdp,
        `b.links.preview({ url: ${JSON.stringify(`${base}/trusted.html`)} })`,
      );
      if (answer.ok) expect(answer.value.title).toBeNull();
      await sleep(500);
      expect(siteHits.length).toBe(hits);
    });

    it("diagnostics: the copied bundle carries no secret the app was started with", async () => {
      const running = app as RunningApp;
      const copied = await bridge<{ copied: boolean; bytes: number }>(
        running.cdp,
        `b.diagnostics.copy()`,
      );
      expect(copied).toMatchObject({ ok: true, value: { copied: true } });
      const text = await bridge<{ text: string }>(running.cdp, `b.clipboard.readText()`);
      if (!text.ok) throw new Error(text.error);
      const bundle = text.value.text;
      expect(bundle.length).toBeGreaterThan(100);
      expect(bundle).not.toContain(PROVIDER_KEY);
      expect(bundle).not.toContain(path.join(work, "user-home"));
    });

    it("CSP: an injected inline script does not run in the app page", async () => {
      const running = app as RunningApp;
      // DevTools' own evaluation is exempt from CSP, so the probe is a script element the page
      // itself would run: the policy (no 'unsafe-inline') must refuse it and report the violation
      const outcome = await running.cdp.eval<{ ran: boolean; violation: string | null }>(
        `new Promise((resolve) => {
          let violation = null;
          document.addEventListener("securitypolicyviolation", (e) => { violation = e.violatedDirective; }, { once: true });
          const script = document.createElement("script");
          script.textContent = "window.__inlineRan = true";
          document.head.append(script);
          setTimeout(() => resolve({ ran: window.__inlineRan === true, violation }), 200);
        })`,
      );
      expect(outcome.ran).toBe(false);
      expect(outcome.violation ?? "").toMatch(/^script-src/);
    });
  },
);
