// The control client's Windows branch (docs/notes/P25/windows-gateway.md): the daemon serves the
// control wire on a named pipe whose name the pointer file holds. Name rules and pointer reading run
// everywhere; the real pipe exchange needs Windows.

import { randomBytes } from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { controlQuery, homeIsSafe, isPipeName, POINTER_FILE, pipeNameFor } from "./control.ts";

const PREFIX = "\\\\.\\pipe\\indus-gw-";
const homes: string[] = [];

function newHome(): string {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "igw-"));
  homes.push(home);
  return home;
}

function newPipeName(): string {
  return PREFIX + randomBytes(16).toString("hex");
}

afterEach(() => {
  for (const home of homes.splice(0)) fs.rmSync(home, { recursive: true, force: true });
});

describe("control pipe names", () => {
  it("accepts only the daemon's pipe names", () => {
    expect(isPipeName(newPipeName())).toBe(true);
    for (const text of [
      "",
      `${PREFIX}${"A".repeat(32)}`,
      `${PREFIX}${"a".repeat(31)}`,
      `${PREFIX}${"a".repeat(32)}x`,
      `\\\\.\\pipe\\other-${"a".repeat(32)}`,
      `//./pipe/indus-gw-${"a".repeat(32)}`,
      `\\\\server\\pipe\\indus-gw-${"a".repeat(32)}`,
      "C:\\Users\\me\\.indus\\gateway.sock",
    ]) {
      expect(isPipeName(text)).toBe(false);
    }
  });

  it("follows the pointer only when it is a file naming a pipe", () => {
    const home = newHome();
    const pointer = path.join(home, POINTER_FILE);
    expect(pipeNameFor(home)).toBeNull();
    const name = newPipeName();
    fs.writeFileSync(pointer, `${name}\n`);
    expect(pipeNameFor(home)).toBe(name);
    fs.writeFileSync(pointer, "C:\\Windows\\System32\\cmd.exe");
    expect(pipeNameFor(home)).toBeNull();
    fs.rmSync(pointer);
    fs.mkdirSync(pointer);
    expect(pipeNameFor(home)).toBeNull();
  });

  it("a Windows home only has to be a directory", () => {
    const home = newHome();
    expect(homeIsSafe(home, "win32")).toBe(true);
    expect(homeIsSafe(path.join(home, "missing"), "win32")).toBe(false);
    const file = path.join(home, "file");
    fs.writeFileSync(file, "");
    expect(homeIsSafe(file, "win32")).toBe(false);
  });
});

describe.skipIf(process.platform !== "win32")("the real pipe (Windows)", () => {
  it("asks the daemon over the pipe the pointer names", async () => {
    const home = newHome();
    const name = newPipeName();
    const seen: string[] = [];
    const server = net.createServer((conn) => {
      conn.setEncoding("utf8");
      conn.on("data", (chunk: string) => {
        seen.push(chunk);
        conn.end(`${JSON.stringify({ ok: true, protocol: 1, id: "x", result: { pid: 4321 } })}\n`);
      });
    });
    await new Promise<void>((resolve) => server.listen(name, resolve));
    try {
      fs.writeFileSync(path.join(home, POINTER_FILE), name);
      expect(await controlQuery(home, "identify")).toEqual({ ok: true, result: { pid: 4321 } });
      const request = JSON.parse(seen.join("").trim()) as Record<string, unknown>;
      expect(request.protocol).toBe(1);
      expect(request.verb).toBe("identify");
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("a pointer to a pipe nobody serves is no_socket", async () => {
    const home = newHome();
    fs.writeFileSync(path.join(home, POINTER_FILE), newPipeName());
    expect(await controlQuery(home, "identify")).toEqual({ ok: false, error: "no_socket" });
  });
});
