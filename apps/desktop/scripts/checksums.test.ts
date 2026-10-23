import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  checkSums,
  formatSums,
  linuxArtefacts,
  linuxChecksums,
  parseSums,
  SUMS_FILE,
  writeSums,
  // @ts-expect-error -- plain JS module without types
} from "./checksums.mjs";

let dir: string;
beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "checksums-test-"));
});
afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

function artefact(name: string, body: string): string {
  const file = path.join(dir, name);
  fs.writeFileSync(file, body);
  return file;
}

const hex = (body: string) => createHash("sha256").update(body).digest("hex");

describe("Linux SHA-256 sums", () => {
  it("lists every Linux artefact the build made, sorted, in sha256sum format", () => {
    const paths = [
      artefact("Indus-1.2.3-linux-x86_64.AppImage", "appimage"),
      artefact("Indus-1.2.3-linux-amd64.deb", "deb"),
      artefact("Indus-1.2.3-linux-arm64.AppImage", "appimage-arm"),
      artefact("latest-linux.yml", "feed"),
    ];
    const returned = linuxChecksums({ artifactPaths: paths });
    expect(returned).toEqual([path.join(dir, SUMS_FILE)]);
    const text = fs.readFileSync(path.join(dir, SUMS_FILE), "utf8");
    expect(text).toBe(
      [
        `${hex("deb")}  Indus-1.2.3-linux-amd64.deb`,
        `${hex("appimage-arm")}  Indus-1.2.3-linux-arm64.AppImage`,
        `${hex("appimage")}  Indus-1.2.3-linux-x86_64.AppImage`,
        "",
      ].join("\n"),
    );
    const listed = parseSums(text).map((e: { name: string }) => e.name);
    for (const file of linuxArtefacts(paths) as string[]) {
      expect(listed).toContain(path.basename(file));
    }
    expect(listed).not.toContain(SUMS_FILE);
    expect(checkSums(path.join(dir, SUMS_FILE))).toEqual([]);
  });

  it("is accepted by sha256sum -c where the tool exists", () => {
    artefact("Indus-1.2.3-linux-x86_64.AppImage", "appimage");
    artefact("Indus-1.2.3-linux-amd64.deb", "deb");
    writeSums(fs.readdirSync(dir).map((n) => path.join(dir, n)));
    const probe = spawnSync("sha256sum", ["--version"], { encoding: "utf8" });
    if (probe.status !== 0) return; // not on this host (macOS ships shasum)
    const run = spawnSync("sha256sum", ["-c", SUMS_FILE], { cwd: dir, encoding: "utf8" });
    expect(run.status).toBe(0);
    expect(run.stdout).toContain("Indus-1.2.3-linux-amd64.deb: OK");
  });

  it("writes nothing on Windows or macOS builds and never lists itself", () => {
    const paths = [artefact("Indus-Setup-1.2.3-x64.exe", "exe"), artefact("latest.yml", "feed")];
    expect(linuxChecksums({ artifactPaths: paths })).toEqual([]);
    expect(fs.existsSync(path.join(dir, SUMS_FILE))).toBe(false);
    const appimage = artefact("a.AppImage", "x");
    const sums = writeSums([appimage]) as string;
    expect(writeSums([appimage, sums])).toBe(sums);
    expect(parseSums(fs.readFileSync(sums, "utf8"))).toHaveLength(1);
  });

  it("fails on a missing artefact, and the check names a changed or missing file", () => {
    expect(() => writeSums([path.join(dir, "gone.deb")])).toThrow(/does not exist/);
    const deb = artefact("x.deb", "one");
    const appimage = artefact("x.AppImage", "two");
    const sums = writeSums([deb, appimage]) as string;
    fs.writeFileSync(deb, "changed");
    fs.rmSync(appimage);
    expect(checkSums(sums)).toEqual(["x.AppImage: missing", "x.deb: digest differs"]);
  });

  it("parses only well-formed lines and formats an empty list as empty", () => {
    expect(formatSums([])).toBe("");
    expect(() => parseSums("abc  file\n")).toThrow(/bad line/);
    expect(parseSums(`${hex("a")} *bin.deb\n`)).toEqual([{ digest: hex("a"), name: "bin.deb" }]);
  });
});
