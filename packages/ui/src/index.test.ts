import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { version } from "./index.ts";

const repoRoot = new URL("../../../", import.meta.url);

function readRepoFile(name: string): string {
  return readFileSync(new URL(name, repoRoot), "utf8");
}

describe("@indus/ui", () => {
  it("exports a semver version", () => {
    expect(version).toMatch(/^\d+\.\d+\.\d+$/);
  });
});

describe("node engine alignment", () => {
  const nvmrc = readRepoFile(".nvmrc").trim();
  const manifest = JSON.parse(readRepoFile("package.json")) as { engines?: { node?: string } };
  const range = manifest.engines?.node ?? "";

  it(".nvmrc holds one exact version", () => {
    expect(nvmrc).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it("engines.node starts at the .nvmrc version and stays on its major", () => {
    const major = nvmrc.split(".")[0];
    expect(range).toBe(`>=${nvmrc} <${Number(major) + 1}`);
  });
});
