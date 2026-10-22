import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import type { MainContext } from "./context.ts";
import {
  buildDiagnostics,
  DIAGNOSTICS_MAX_BYTES,
  type DiagnosticsInput,
  installDiagnostics,
  NOTICES_FILE,
  NOTICES_MAX_BYTES,
  RENDERER_ERRORS_PER_MINUTE,
  RendererErrors,
  readBuildStamp,
  readNotices,
} from "./diagnostics.ts";
import { DesktopLog, type LogFs } from "./log.ts";

// Fake secrets are built at run time so no credential-shaped literal sits in the source.
const rep = (c: string, n: number) => c.repeat(n);
const SECRETS = {
  openai: `sk-${rep("A1b2", 10)}`,
  github: `gh${"p"}_${rep("Zx9", 12)}`,
  slack: `xox${"b"}-${rep("1234", 4)}-${rep("q", 20)}`,
  aws: `AKI${"A"}${rep("Q", 16)}`,
  bearer: rep("bearerpart", 3),
  jwt: `ey${"J"}${rep("h", 12)}.${rep("p", 20)}.${rep("s", 20)}`,
  ticket: rep("t1ck", 8),
  urlPassword: rep("pw", 8),
  serveToken: rep("m1nted", 5),
  envValue: rep("envsecret", 3),
};
/** Any of these left in a bundle means a leak. */
const TOKEN_PATTERN =
  /sk-[A-Za-z0-9_-]{16,}|gh[opsur]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}/;

function leakyLog(): string[] {
  return [
    `provider call failed with key ${SECRETS.openai}`,
    `git push used ${SECRETS.github}`,
    `slack token ${SECRETS.slack} and aws ${SECRETS.aws}`,
    `Authorization: Bearer ${SECRETS.bearer}`,
    `jwt ${SECRETS.jwt}`,
    `offered indus-gateway-ticket.${SECRETS.ticket}`,
    `remote https://me:${SECRETS.urlPassword}@example.com/api`,
    `GET /api/status?token=${SECRETS.serveToken}`,
    `serve token in plain text ${SECRETS.serveToken}`,
    `OPENAI_API_KEY=${SECRETS.envValue}`,
  ];
}

const HOME = path.join(os.tmpdir(), "home-of-someone");

function input(overrides: Partial<DiagnosticsInput> = {}): DiagnosticsInput {
  return {
    facts: {
      appVersion: "0.4.2",
      platform: "linux",
      arch: "x64",
      packaged: true,
      dev: false,
      versions: { electron: "44.5.1", chrome: "150.0", node: "24.21.0" },
    },
    stamp: { version: "0.4.2", commit: "abc1234", channel: "canary", updateOwner: "manual" },
    paths: {
      home: path.join(HOME, ".indus"),
      desktopLog: path.join(HOME, ".indus/logs/desktop.log"),
    },
    homeDir: HOME,
    env: { INDUS_HOME: path.join(HOME, ".indus"), INDUS_API_TOKEN: SECRETS.envValue, PATH: "/bin" },
    windowCount: 2,
    rendererErrors: [`[renderer] main (window 3): fetch failed for key ${SECRETS.openai}`],
    logLines: leakyLog(),
    knownSecrets: [SECRETS.serveToken, SECRETS.ticket],
    now: new Date("2026-10-21T08:00:00.000Z"),
    ...overrides,
  };
}

describe("diagnostics bundle", () => {
  it("contains no token pattern and none of the secret values", () => {
    const text = buildDiagnostics(input());
    expect(text).not.toMatch(TOKEN_PATTERN);
    for (const [name, value] of Object.entries(SECRETS)) {
      expect(text.includes(value), `${name} leaked`).toBe(false);
    }
    expect(text).toContain("[redacted]");
  });

  it("lists sections, versions, the stamp and INDUS_* names without values", () => {
    const text = buildDiagnostics(input());
    for (const title of [
      "## App",
      "## Build stamp",
      "## Paths",
      "## Environment (names only)",
      "## Renderer errors",
      "## Recent log",
    ]) {
      expect(text).toContain(title);
    }
    expect(text).toContain("electron: 44.5.1");
    expect(text).toContain("updateOwner: manual");
    expect(text).toContain("INDUS_API_TOKEN\n");
    expect(text).not.toContain("PATH");
    expect(text).toContain("windows: 2");
  });

  it("shows the home folder as ~", () => {
    const text = buildDiagnostics(input());
    expect(text).not.toContain(HOME);
    expect(text).toContain("home: ~/.indus");
  });

  it("says when there is no build stamp, and redacts extra sections too", () => {
    const text = buildDiagnostics(
      input({
        stamp: null,
        sections: [{ title: "Connection", lines: [`token=${SECRETS.envValue}`] }],
      }),
    );
    expect(text).toContain("none (source checkout)");
    expect(text).toContain("## Connection");
    expect(text).not.toContain(SECRETS.envValue);
  });

  it("stays under the size cap by dropping the oldest log lines", () => {
    const lines = Array.from({ length: 300 }, (_, i) => `line ${i} ${"x".repeat(2000)}`);
    const text = buildDiagnostics(input({ logLines: lines }));
    expect(Buffer.byteLength(text)).toBeLessThanOrEqual(DIAGNOSTICS_MAX_BYTES);
    expect(text).toContain("line 299 ");
    expect(text).not.toContain("line 0 ");
  });

  it("reads the stamp beside the archive, null when missing", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-stamp-read-"));
    dirs.push(dir);
    expect(readBuildStamp(dir)).toBeNull();
    expect(readBuildStamp(null)).toBeNull();
    fs.writeFileSync(
      path.join(dir, "desktop-build-stamp.json"),
      JSON.stringify({ version: "1.0.0" }),
    );
    expect(readBuildStamp(dir)).toEqual({ version: "1.0.0" });
  });

  it("reads the packaged notices for the licences screen; null in a dev run or when unfit (W46)", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-notices-read-"));
    dirs.push(dir);
    expect(readNotices(null)).toBeNull();
    expect(readNotices(dir)).toBeNull(); // not packaged beside the app
    fs.writeFileSync(path.join(dir, NOTICES_FILE), "# Third-party notices\n\nsome-lib MIT\n");
    expect(readNotices(dir)).toBe("# Third-party notices\n\nsome-lib MIT\n");
    fs.writeFileSync(path.join(dir, NOTICES_FILE), "x".repeat(NOTICES_MAX_BYTES + 1));
    expect(readNotices(dir)).toBeNull(); // far past the real file's size: not ours
    fs.rmSync(path.join(dir, NOTICES_FILE));
    fs.mkdirSync(path.join(dir, NOTICES_FILE));
    expect(readNotices(dir)).toBeNull(); // a directory, not a file
  });

  it("the packager config ships the notices file under the name main reads", () => {
    const config = fs.readFileSync(
      new URL("../electron-builder.config.cjs", import.meta.url),
      "utf8",
    );
    expect(config).toContain(`to: "${NOTICES_FILE}"`);
  });
});

describe("renderer errors", () => {
  it("are written at ERROR with source, window and a cut stack", () => {
    const lines: Array<[string, string]> = [];
    const errors = new RendererErrors({ write: (m, l) => lines.push([m, l]), now: () => 0 });
    const stack = Array.from({ length: 40 }, (_, i) => `at frame${i} (app.js:${i})`).join("\n");
    expect(errors.record(7, { message: "boom\nsecond line", stack, source: "renderer:main" })).toBe(
      true,
    );
    const [text, level] = lines[0] ?? ["", ""];
    expect(level).toBe("ERROR");
    expect(text.split("\n")[0]).toBe("[renderer] renderer:main (window 7): boom second line");
    expect(text.split("\n")).toHaveLength(21);
    expect(errors.recent()).toEqual(["[renderer] renderer:main (window 7): boom second line"]);
  });

  it(`stop after ${RENDERER_ERRORS_PER_MINUTE} per window per minute, with one suppression line`, () => {
    const lines: Array<[string, string]> = [];
    let now = 0;
    const errors = new RendererErrors({ write: (m, l) => lines.push([m, l]), now: () => now });
    for (let i = 0; i < 30; i += 1) errors.record(1, { message: `e${i}` });
    expect(lines.filter(([, l]) => l === "ERROR")).toHaveLength(RENDERER_ERRORS_PER_MINUTE);
    expect(lines.filter(([m]) => m.includes("suppressed"))).toHaveLength(1);
    expect(errors.record(2, { message: "other window" })).toBe(true);
    now = 60_000;
    expect(errors.record(1, { message: "next minute" })).toBe(true);
  });
});

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function memoryFs(files: Map<string, string>): LogFs {
  return {
    appendFileSync: (file, data) => files.set(file, (files.get(file) ?? "") + data),
    statSize: (file) => (files.has(file) ? Buffer.byteLength(files.get(file) ?? "") : null),
    renameSync: () => {},
    rmSync: () => {},
    mkdirSync: () => {},
  };
}

type Handler = (payload: unknown, context: { senderId: number }) => unknown;

function harness() {
  const files = new Map<string, string>();
  const log = new DesktopLog({
    file: "/logs/desktop.log",
    fs: memoryFs(files),
    timers: { setTimeout: () => 0, clearTimeout: () => {} },
  });
  const handlers = new Map<string, Handler>();
  const registry = {
    handle: (name: string, fn: Handler) => handlers.set(name, fn),
    on: (name: string, fn: Handler) => handlers.set(name, fn),
  };
  const clipboard = {
    text: "",
    readText: () => clipboard.text,
    writeText: (t: string) => {
      clipboard.text = t;
    },
  };
  const revealed: string[] = [];
  const context = {
    registry,
    log,
    paths: { home: "/u/.indus", userData: "/u/.config/Indus", desktopLog: "/logs/desktop.log" },
    windows: { windows: () => [{}, {}] },
    appVersion: "0.4.2",
    platform: "linux",
    packaged: false,
    dev: true,
  } as unknown as MainContext;
  const handle = installDiagnostics(context, {
    clipboard,
    revealPath: (file) => revealed.push(file),
    facts: { arch: "x64", versions: { electron: "44.5.1" } },
    homeDir: "/u",
    env: {},
    resourcesPath: null,
    knownSecrets: () => [SECRETS.serveToken],
  });
  return { files, log, handlers, clipboard, revealed, handle };
}

describe("installDiagnostics", () => {
  it("a renderer error appears in desktop.log", () => {
    const { files, log, handlers } = harness();
    handlers.get("indus:logs:renderer-error")?.(
      { message: `render failed with ${SECRETS.openai}`, stack: "at App (app.js:1)" },
      { senderId: 4 },
    );
    log.flush();
    const text = files.get("/logs/desktop.log") ?? "";
    expect(text).toMatch(
      /ERROR desktop: \[renderer\] renderer \(window 4\): render failed with \[redacted\]/,
    );
    expect(text).toContain("ERROR desktop:     at App (app.js:1)");
    expect(text).not.toContain(SECRETS.openai);
  });

  it("about.notices answers null when the app is not packaged", async () => {
    const { handlers } = harness();
    expect(await handlers.get("indus:about:notices")?.({}, { senderId: 1 })).toEqual({
      text: null,
    });
  });

  it("logs.recent returns the kept lines; logs.reveal shows the log file", async () => {
    const { log, handlers, revealed } = harness();
    for (let i = 0; i < 5; i += 1) log.write(`line ${i}`);
    const recent = (await handlers.get("indus:logs:recent")?.({ limit: 2 }, { senderId: 1 })) as {
      lines: string[];
    };
    expect(recent.lines).toHaveLength(2);
    expect(recent.lines[1]).toContain("line 4");
    expect(await handlers.get("indus:logs:reveal")?.({}, { senderId: 1 })).toEqual({ ok: true });
    expect(revealed).toEqual(["/logs/desktop.log"]);
  });

  it("copy diagnostics puts the redacted bundle on the clipboard and answers only its size", async () => {
    const { log, handlers, clipboard, handle } = harness();
    log.write(`minted ${SECRETS.serveToken}`);
    log.write(`key ${SECRETS.github}`);
    const answer = (await handlers.get("indus:diagnostics:copy")?.({}, { senderId: 1 })) as {
      copied: boolean;
      bytes: number;
    };
    expect(answer).toEqual({ copied: true, bytes: Buffer.byteLength(clipboard.text) });
    expect(Object.keys(answer)).toEqual(["copied", "bytes"]);
    expect(clipboard.text).toContain("# Indus desktop diagnostics");
    expect(clipboard.text).not.toMatch(TOKEN_PATTERN);
    expect(clipboard.text).not.toContain(SECRETS.serveToken);
    expect(clipboard.text).toContain("windows: 2");
    expect(handle.menuItems.map((item) => item.label)).toEqual([
      "Copy Diagnostics",
      "Show Log File",
    ]);
  });

  it("the clipboard bridge reads and writes text", async () => {
    const { handlers, clipboard } = harness();
    expect(
      await handlers.get("indus:clipboard:write-text")?.({ text: "hello" }, { senderId: 1 }),
    ).toEqual({
      ok: true,
    });
    expect(clipboard.text).toBe("hello");
    expect(await handlers.get("indus:clipboard:read-text")?.({}, { senderId: 1 })).toEqual({
      text: "hello",
    });
  });
});
