import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { closeFakeServes, fakeServe, SERVE_TOKEN } from "../../testing/fakeServe.ts";
import { ConnectionsService } from "./install.ts";
import { REGISTRY_FILE, secretName } from "./registry.ts";

const dirs: string[] = [];
afterAll(() => {
  closeFakeServes();
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function service() {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "indus-conns-"));
  dirs.push(userData);
  const values = new Map<string, string>();
  const secrets = {
    get: (name: string) => values.get(name) ?? null,
    put: (name: string, value: string) => void values.set(name, value),
    delete: (name: string) => void values.delete(name),
  };
  const lines: string[] = [];
  const conns = new ConnectionsService({
    userData,
    secrets,
    log: (line) => lines.push(line),
    test: { settleMs: 30, timeoutMs: 2000 },
  });
  return { conns, values, userData, lines };
}

describe("ConnectionsService", () => {
  it("saves only after both legs pass; a failed test writes no secret", async () => {
    const { conns, values } = service();
    const serve = await fakeServe();
    const refused = await conns.save({ kind: "remote", url: serve.url, token: "wrong" });
    expect(refused).toMatchObject({ saved: false, test: { http: "unauthorized" } });
    expect(values.size).toBe(0);
    expect(conns.view().connections).toHaveLength(1);
    const ok = await conns.save({
      kind: "remote",
      url: serve.url,
      token: SERVE_TOKEN,
      label: "Lab",
    });
    expect(ok).toMatchObject({ saved: true, test: { ok: true } });
    expect(values.get(secretName(ok.id as string, "token"))).toBe(SERVE_TOKEN);
  });

  it("the view and the file carry names and flags, never a credential", async () => {
    const { conns, userData } = service();
    const serve = await fakeServe();
    const ok = await conns.save({
      kind: "remote",
      url: serve.url,
      token: SERVE_TOKEN,
      headers: { "CF-Access-Client-Secret": "cf-value-9" },
    });
    const view = conns.view();
    const entry = view.connections.find((c) => c.id === ok.id);
    expect(entry).toMatchObject({ hasCredential: true, headerNames: ["CF-Access-Client-Secret"] });
    const all = JSON.stringify(view) + fs.readFileSync(path.join(userData, REGISTRY_FILE), "utf8");
    expect(all).not.toContain(SERVE_TOKEN);
    expect(all).not.toContain("cf-value-9");
  });

  it("a blank token on an edit keeps the stored one; the launch dial comes from the primary", async () => {
    const { conns, values } = service();
    const serve = await fakeServe();
    const ok = await conns.save({ kind: "remote", url: serve.url, token: SERVE_TOKEN });
    const again = await conns.save({
      id: ok.id as string,
      kind: "remote",
      url: serve.url,
      token: "",
      label: "Renamed",
    });
    expect(again.saved).toBe(true);
    expect(values.get(secretName(ok.id as string, "token"))).toBe(SERVE_TOKEN);
    expect(conns.launchRemote()).toBeNull(); // "This computer" is still the primary
    conns.setPrimary(ok.id as string);
    const dial = conns.launchRemote();
    expect(dial).toMatchObject({ url: serve.url, connectionId: ok.id });
    expect(dial?.auth).toBeDefined();
  });

  it("the first-run chooser's remote becomes the primary entry; forget removes it", () => {
    const { conns, values } = service();
    const id = conns.adoptRemote("http://r.example:9119", SERVE_TOKEN);
    expect(conns.primaryRemote()).toEqual({ url: "http://r.example:9119", hasToken: true });
    expect(conns.view().primaryId).toBe(id);
    conns.forgetPrimaryRemote();
    expect(conns.primaryRemote()).toEqual({ url: null, hasToken: false });
    expect(values.size).toBe(0);
  });

  it("an OAuth entry is saved before its sign-in and is not dialled until signed in", async () => {
    const { conns } = service();
    const ok = await conns.save({ kind: "remote", url: "https://gw.example", authMode: "oauth" });
    expect(ok).toMatchObject({ saved: true, test: null });
    conns.setPrimary(ok.id as string);
    expect(conns.view().connections.find((c) => c.id === ok.id)?.signedIn).toBe(false);
    const dial = conns.launchRemote();
    await expect(dial?.auth?.authHeaders()).rejects.toMatchObject({ kind: "unauthorized" });
  });
});
