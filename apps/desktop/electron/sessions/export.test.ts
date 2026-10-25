import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import type { ApiRequest, ApiResult } from "../api/proxy.ts";
import { IpcError } from "../ipc/envelope.ts";
import { SCHEMAS } from "../ipc/schemas.ts";
import { exportFileName, exportSession, type SessionExportDeps, toJsonLines } from "./export.ts";

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

const OBJECTS = [
  { kind: "session", format: "indus.session/1", id: "s-1", title: "Plan" },
  { kind: "message", session_id: "s-1", role: "user", content: "hello" },
];

function deps(
  answer: ApiResult | (() => Promise<ApiResult>),
  choose: string | null,
): SessionExportDeps & { asked: ApiRequest[]; dialogs: Array<[number, string]>; lines: string[] } {
  const asked: ApiRequest[] = [];
  const dialogs: Array<[number, string]> = [];
  const lines: string[] = [];
  return {
    asked,
    dialogs,
    lines,
    request: async (request) => {
      asked.push(request);
      return typeof answer === "function" ? answer() : answer;
    },
    chooseFile: async (senderId, defaultPath) => {
      dialogs.push([senderId, defaultPath]);
      return choose;
    },
    downloadsDir: () => "/home/person/Downloads",
    log: (line) => lines.push(line),
  };
}

const ok = (body: unknown): ApiResult => ({ kind: "response", status: 200, body });

describe("session export (W56-G1)", () => {
  it("reads the export over the proxy, asks for a file in the sender's window, writes JSON Lines", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-export-"));
    dirs.push(dir);
    const target = path.join(dir, "mine.jsonl");
    const d = deps(ok({ format: "indus.session/1", session_id: "s-1", objects: OBJECTS }), target);
    const answer = await exportSession(d, 7, "s-1");
    expect(answer).toEqual({ saved: true, fileName: "mine.jsonl" });
    expect(d.asked).toEqual([
      { method: "GET", path: "/api/sessions/s-1/export", timeoutMs: 120_000 },
    ]);
    // the connection's profile comes from main's proxy target, never from the window
    expect(d.asked[0]?.profile).toBeUndefined();
    expect(d.dialogs).toEqual([
      [7, path.join("/home/person/Downloads", "indus-session-s-1.jsonl")],
    ]);
    const lines = fs.readFileSync(target, "utf8").trimEnd().split("\n");
    expect(lines.map((line) => JSON.parse(line))).toEqual(OBJECTS);
    if (process.platform !== "win32") expect(fs.statSync(target).mode & 0o777).toBe(0o600);
    expect(d.lines.join("\n")).not.toContain("hello");
  });

  it("an existing file is replaced and tightened to 0600", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-export-"));
    dirs.push(dir);
    const target = path.join(dir, "old.jsonl");
    fs.writeFileSync(target, "older", { mode: 0o644 });
    await exportSession(deps(ok({ objects: OBJECTS }), target), 1, "s-1");
    expect(fs.readFileSync(target, "utf8")).toContain('"role":"user"');
    if (process.platform !== "win32") expect(fs.statSync(target).mode & 0o777).toBe(0o600);
  });

  it("a cancelled dialog writes nothing", async () => {
    const written: string[] = [];
    const d = {
      ...deps(ok({ objects: OBJECTS }), null),
      write: (file: string) => written.push(file),
    };
    expect(await exportSession(d, 3, "s-1")).toEqual({ saved: false });
    expect(written).toEqual([]);
  });

  it("the id is encoded in the route and made safe in the default name", async () => {
    const d = deps(ok({ objects: OBJECTS }), null);
    await exportSession(d, 3, "a/b c");
    expect(d.asked[0]?.path).toBe("/api/sessions/a%2Fb%20c/export");
    expect(exportFileName("a/b c")).toBe("indus-session-a_b_c.jsonl");
    expect(exportFileName("../..")).toBe("indus-session-.._...jsonl"); // no separator survives
  });

  it("server failures become errors before any dialog opens", async () => {
    const missing = deps({ kind: "not-found", status: 404, body: null }, "/x");
    await expect(exportSession(missing, 1, "gone")).rejects.toMatchObject({ code: "not-found" });
    expect(missing.dialogs).toEqual([]);
    const large = deps(
      {
        kind: "response",
        status: 413,
        body: { detail: { code: "export_too_large", message: "too large here" } },
      },
      "/x",
    );
    await expect(exportSession(large, 1, "big")).rejects.toMatchObject({
      code: "too-large",
      message: "too large here",
    });
    const empty = deps(ok({ objects: [] }), "/x");
    await expect(exportSession(empty, 1, "s")).rejects.toBeInstanceOf(IpcError);
    expect(empty.dialogs).toEqual([]);
    const offline = deps(async () => {
      throw new IpcError("not-connected", "Indus is not connected yet");
    }, "/x");
    await expect(exportSession(offline, 1, "s")).rejects.toMatchObject({ code: "not-connected" });
  });

  it("a failed write answers save-failed", async () => {
    const d = {
      ...deps(ok({ objects: OBJECTS }), "/nowhere/x.jsonl"),
      write: () => {
        throw new Error("read-only");
      },
    };
    await expect(exportSession(d, 1, "s-1")).rejects.toMatchObject({ code: "save-failed" });
  });

  it("the window may send only a session id (no path, no profile)", () => {
    const request = SCHEMAS["indus:sessions:export"].request;
    expect(request.safeParse({ sessionId: "s-1" }).success).toBe(true);
    expect(request.safeParse({ sessionId: "s-1", path: "/etc/passwd" }).success).toBe(false);
    expect(request.safeParse({ sessionId: "s-1", profile: "work" }).success).toBe(false);
    expect(request.safeParse({ sessionId: "" }).success).toBe(false);
  });

  it("JSON Lines: one object per line", () => {
    expect(toJsonLines({ objects: [{ a: 1 }, { b: "x\ny" }] })).toBe('{"a":1}\n{"b":"x\\ny"}\n');
    expect(() => toJsonLines(null)).toThrow(IpcError);
  });
});
