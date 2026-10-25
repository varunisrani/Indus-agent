// The desktop picks the same Indus home as the `indus` CLI (`indus.foundation.home`).

import path from "node:path";
import { describe, expect, it } from "vitest";
import { legacyWindowsHome, resolveIndusHome } from "./paths.ts";

const WIN_HOME = "C:\\Users\\me";
const LOCAL = "C:\\Users\\me\\AppData\\Local";

describe("the Indus home", () => {
  it("INDUS_HOME wins everywhere when it is absolute", () => {
    const explicit = path.resolve("/srv/indus-home");
    expect(resolveIndusHome({ INDUS_HOME: explicit }, "/home/me", "linux")).toBe(explicit);
    expect(resolveIndusHome({ INDUS_HOME: explicit, LOCALAPPDATA: LOCAL }, WIN_HOME, "win32")).toBe(
      explicit,
    );
  });

  it("POSIX keeps ~/.indus", () => {
    expect(resolveIndusHome({}, "/home/me", "linux")).toBe(path.join("/home/me", ".indus"));
    expect(resolveIndusHome({}, "/Users/me", "darwin")).toBe(path.join("/Users/me", ".indus"));
  });

  it("Windows uses %LOCALAPPDATA%\\indus, as the CLI and the installer do", () => {
    expect(resolveIndusHome({ LOCALAPPDATA: LOCAL }, WIN_HOME, "win32")).toBe(`${LOCAL}\\indus`);
    expect(resolveIndusHome({}, WIN_HOME, "win32")).toBe(`${LOCAL}\\indus`);
  });

  it("Windows appends the CLI's data-dir suffix, ignoring one that is a path", () => {
    const env = { LOCALAPPDATA: LOCAL, INDUS_DATA_DIR_SUFFIX: "-dev" };
    expect(resolveIndusHome(env, WIN_HOME, "win32")).toBe(`${LOCAL}\\indus-dev`);
    const bad = { LOCALAPPDATA: LOCAL, INDUS_DATA_DIR_SUFFIX: "..\\x" };
    expect(resolveIndusHome(bad, WIN_HOME, "win32")).toBe(`${LOCAL}\\indus`);
  });
});

describe("settings left in ~/.indus by an earlier Windows build", () => {
  const home = `${LOCAL}\\indus`;
  const legacy = `${WIN_HOME}\\.indus`;
  const files =
    (...present: string[]) =>
    (file: string) =>
      present.includes(file);

  it("is named when it holds settings and the home has none", () => {
    const exists = files(`${legacy}\\config.yaml`);
    expect(legacyWindowsHome(home, {}, WIN_HOME, "win32", exists)).toBe(legacy);
  });

  it("is not named once the home has settings, or nothing was left", () => {
    const both = files(`${legacy}\\state.db`, `${home}\\config.yaml`);
    expect(legacyWindowsHome(home, {}, WIN_HOME, "win32", both)).toBeNull();
    expect(legacyWindowsHome(home, {}, WIN_HOME, "win32", files())).toBeNull();
  });

  it("is never named off Windows or with INDUS_HOME set", () => {
    const exists = files(`${legacy}\\config.yaml`);
    expect(legacyWindowsHome(home, {}, WIN_HOME, "linux", exists)).toBeNull();
    expect(legacyWindowsHome(home, { INDUS_HOME: home }, WIN_HOME, "win32", exists)).toBeNull();
  });
});
