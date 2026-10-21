import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import {
  desktopEntry,
  installLinuxLauncher,
  type LauncherDeps,
  quoteExecArg,
  writeLinuxLauncher,
} from "./linuxLauncher.ts";

const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function setup(overrides: Partial<LauncherDeps> = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "indus-launcher-"));
  dirs.push(root);
  const resources = path.join(root, "resources");
  fs.mkdirSync(path.join(resources, "icons"), { recursive: true });
  fs.writeFileSync(path.join(resources, "icons", "indus.png"), "png");
  const lines: string[] = [];
  const deps: LauncherDeps = {
    platform: "linux",
    packaged: true,
    env: { APPIMAGE: path.join(root, "Apps", "Indus 0.1.AppImage") },
    homeDir: path.join(root, "home"),
    resourcesPath: resources,
    scheme: "indus",
    log: (line) => lines.push(line),
    ...overrides,
  };
  const entry = path.join(
    root,
    "home",
    ".local",
    "share",
    "applications",
    "ai.indus.desktop.desktop",
  );
  return { root, deps, lines, entry };
}

describe("Linux launcher entry", () => {
  it("quotes the AppImage path for the Exec line", () => {
    expect(quoteExecArg("/opt/Indus.AppImage")).toBe("/opt/Indus.AppImage");
    expect(quoteExecArg('/home/a b/$x"`\\.AppImage')).toBe('"/home/a b/\\$x\\"\\`\\\\.AppImage"');
    expect(quoteExecArg("/tmp/100%")).toBe("/tmp/100%%");
  });

  it("registers the deep-link scheme and passes links with %U", () => {
    const text = desktopEntry({ execPath: "/opt/Indus.AppImage", scheme: "indus" });
    expect(text).toContain("Exec=/opt/Indus.AppImage %U\n");
    expect(text).toContain("MimeType=x-scheme-handler/indus;\n");
    expect(text).toContain("Icon=ai.indus.desktop\n");
    expect(text.startsWith("[Desktop Entry]\nType=Application\n")).toBe(true);
  });

  it("writes the entry and the icon once, and does not rewrite an unchanged entry", () => {
    const { deps, entry, root } = setup();
    expect(writeLinuxLauncher(deps)).toEqual({ written: true, file: entry });
    expect(fs.readFileSync(entry, "utf8")).toContain('Exec="');
    const icon = path.join(
      root,
      "home/.local/share/icons/hicolor/256x256/apps/ai.indus.desktop.png",
    );
    expect(fs.readFileSync(icon, "utf8")).toBe("png");
    expect(writeLinuxLauncher(deps)).toEqual({ written: false, reason: "unchanged" });
  });

  it("honours XDG_DATA_HOME", () => {
    const { deps, root } = setup();
    const data = path.join(root, "data");
    const out = writeLinuxLauncher({ ...deps, env: { ...deps.env, XDG_DATA_HOME: data } });
    expect(out).toEqual({
      written: true,
      file: path.join(data, "applications", "ai.indus.desktop.desktop"),
    });
  });

  it("does nothing outside a packaged Linux AppImage", () => {
    expect(writeLinuxLauncher(setup({ platform: "darwin" }).deps)).toEqual({
      written: false,
      reason: "not-linux",
    });
    expect(writeLinuxLauncher(setup({ packaged: false }).deps).written).toBe(false);
    expect(writeLinuxLauncher(setup({ env: {} }).deps)).toEqual({
      written: false,
      reason: "not-appimage",
    });
  });

  it("a write failure is logged, never thrown", () => {
    const { deps, lines } = setup({
      fs: {
        readFile: () => null,
        exists: () => true,
        copyFile: () => {},
        writeFileAtomic: () => {
          throw new Error("read-only file system");
        },
      },
    });
    expect(writeLinuxLauncher(deps)).toEqual({ written: false, reason: "failed" });
    expect(lines.join("\n")).toContain("read-only file system");
  });

  it("waits for the window to be shown", () => {
    const { deps, entry } = setup();
    let show: (() => void) | null = null;
    installLinuxLauncher(
      {
        isVisible: () => false,
        on: (_event: string, listener: () => void) => {
          show = listener;
        },
      },
      deps,
    );
    expect(fs.existsSync(entry)).toBe(false);
    (show as (() => void) | null)?.();
    expect(fs.existsSync(entry)).toBe(true);
    fs.rmSync(entry);
    (show as (() => void) | null)?.(); // a later show does not write again
    expect(fs.existsSync(entry)).toBe(false);
  });
});
