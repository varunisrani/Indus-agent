import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import type { Connection } from "../boot/orchestrator.ts";
import { downloadArtifact, freeName } from "./download.ts";

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

const TOKEN = ["remote", "serve", "credential"].join("-");
const TICKET = ["dl", "ticket", "value"].join("-");

function connection(headers?: Connection["rest"]["headers"]): Connection {
  return {
    descriptor: {
      mode: "remote",
      profile: "work",
      profileId: "p",
      instanceId: "i",
      codeVersion: "0.24.0",
      wsUrl: "wss://gw.example/team/api/ws",
    },
    rest: { origin: "https://gw.example/team", token: TOKEN, ...(headers ? { headers } : {}) },
    ticket: async () => {
      throw new Error("not used");
    },
  };
}

describe("artifact downloads", () => {
  it("mints the ticket with the originating profile and session, then saves the bytes", async () => {
    const downloads = fs.mkdtempSync(path.join(os.tmpdir(), "indus-dl-"));
    dirs.push(downloads);
    fs.writeFileSync(path.join(downloads, "chart.png"), "older");
    const calls: Array<{ url: string; headers: Record<string, string> }> = [];
    const fake: typeof fetch = async (input, init) => {
      const url = String(input);
      calls.push({ url, headers: (init?.headers ?? {}) as Record<string, string> });
      if (url.includes("/download-ticket")) {
        return new Response(JSON.stringify({ ticket: TICKET, expires_in: 30 }), { status: 200 });
      }
      return new Response(Buffer.from("PNGDATA"), { status: 200 });
    };
    const result = await downloadArtifact(
      {
        current: () => connection(async () => ({ Authorization: "Bearer oauth-access" })),
        downloadsDir: () => downloads,
        fetch: fake,
        log: () => {},
      },
      { path: "/srv/work/out/chart.png", profile: "research", sessionId: "s-42" },
    );
    const mint = new URL(calls[0]?.url as string);
    expect(mint.pathname).toBe("/team/api/files/download-ticket");
    expect(mint.searchParams.get("profile")).toBe("research");
    expect(mint.searchParams.get("session_id")).toBe("s-42");
    expect(calls[0]?.headers.Authorization).toBe("Bearer oauth-access");
    const file = new URL(calls[1]?.url as string);
    expect(file.searchParams.get("ticket")).toBe(TICKET);
    expect(file.searchParams.get("profile")).toBe("research");
    expect(result.savedTo).toBe(path.join(downloads, "chart (2).png"));
    expect(fs.readFileSync(result.savedTo, "utf8")).toBe("PNGDATA");
    expect(JSON.stringify(result)).not.toContain(TICKET);
  });

  it("refuses secret files, needs a connection and names a refused credential", async () => {
    const deps = {
      current: () => connection(),
      downloadsDir: () => os.tmpdir(),
      fetch: (async () => new Response("{}", { status: 401 })) as typeof fetch,
      log: () => {},
    };
    await expect(
      downloadArtifact(deps, { path: "/home/u/.indus/.env", profile: null, sessionId: "s" }),
    ).rejects.toMatchObject({ code: "forbidden" });
    await expect(
      downloadArtifact(
        { ...deps, current: () => null },
        { path: "/x", profile: null, sessionId: "s" },
      ),
    ).rejects.toMatchObject({ code: "not-connected" });
    await expect(
      downloadArtifact(deps, { path: "/x/a.txt", profile: null, sessionId: "s" }),
    ).rejects.toMatchObject({ code: "unauthorized" });
  });

  it("file names never escape the folder", () => {
    const dir = os.tmpdir();
    expect(path.dirname(freeName(dir, "../../etc/passwd"))).toBe(dir);
    expect(path.basename(freeName(dir, "a<b>:c.txt"))).toBe("a_b_c.txt");
  });
});
