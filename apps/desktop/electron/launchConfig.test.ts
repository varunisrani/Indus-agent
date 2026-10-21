import { describe, expect, it } from "vitest";
import { chooseOzonePlatform, planSwitches, readLaunchConfig } from "./launchConfig.ts";

describe("readLaunchConfig", () => {
  it("reads a flow list and the heap ceiling", () => {
    const { config, warnings } = readLaunchConfig(
      [
        "model: x",
        "desktop:",
        "  electron_flags: [\"--enable-features=A\", '--disable-gpu-vsync']  # tuning",
        "  renderer_max_old_space_mb: 4096",
        "other: 1",
      ].join("\n"),
    );
    expect(config).toEqual({
      electronFlags: ["--enable-features=A", "--disable-gpu-vsync"],
      rendererMaxOldSpaceMb: 4096,
    });
    expect(warnings).toEqual([]);
  });

  it("reads a block list with four-space items", () => {
    const { config } = readLaunchConfig(
      'desktop:\n  electron_flags:\n    - --a\n    - "--b=1"\n  font: x\n',
    );
    expect(config.electronFlags).toEqual(["--a", "--b=1"]);
  });

  it("reads a single string as space-separated flags", () => {
    expect(
      readLaunchConfig('desktop:\n  electron_flags: "--a --b"\n').config.electronFlags,
    ).toEqual(["--a", "--b"]);
  });

  it("keeps defaults and warns once for an unsupported indentation", () => {
    const { config, warnings } = readLaunchConfig(
      "desktop:\n    electron_flags: [--a]\n    renderer_max_old_space_mb: 9\n",
    );
    expect(config).toEqual({ electronFlags: [], rendererMaxOldSpaceMb: 0 });
    expect(warnings).toHaveLength(1);
  });

  it("is silent when the desktop block does not mention the keys", () => {
    expect(readLaunchConfig("desktop:\n      font: x\n").warnings).toEqual([]);
    expect(readLaunchConfig("").config).toEqual({ electronFlags: [], rendererMaxOldSpaceMb: 0 });
  });

  it("ignores a bad heap value", () => {
    expect(
      readLaunchConfig("desktop:\n  renderer_max_old_space_mb: lots\n").config
        .rendererMaxOldSpaceMb,
    ).toBe(0);
    expect(
      readLaunchConfig("desktop:\n  renderer_max_old_space_mb: -5\n").config.rendererMaxOldSpaceMb,
    ).toBe(0);
  });
});

describe("planSwitches", () => {
  it("merges every js-flags source into one switch", () => {
    const planned = planSwitches(
      {
        electronFlags: ["--js-flags=--expose-gc", "--enable-features=X"],
        rendererMaxOldSpaceMb: 2048,
      },
      ["--js-flags", "--stack-size=900"],
    );
    expect(planned).toEqual([
      { name: "enable-features", value: "X" },
      { name: "js-flags", value: "--stack-size=900 --expose-gc --max-old-space-size=2048" },
    ]);
  });

  it("a heap ceiling on argv beats the config's", () => {
    const planned = planSwitches({ electronFlags: [], rendererMaxOldSpaceMb: 2048 }, [
      "--js-flags=--max-old-space-size=8192",
    ]);
    expect(planned).toEqual([{ name: "js-flags", value: "--max-old-space-size=8192" }]);
  });

  it("does not re-append a switch already on argv", () => {
    expect(
      planSwitches({ electronFlags: ["--disable-gpu"], rendererMaxOldSpaceMb: 0 }, [
        "--disable-gpu",
      ]),
    ).toEqual([]);
  });

  it("plans nothing without config", () => {
    expect(planSwitches({ electronFlags: [], rendererMaxOldSpaceMb: 0 }, ["app.js"])).toEqual([]);
  });
});

describe("chooseOzonePlatform", () => {
  const linux = (env: Record<string, string>, argv: string[] = []) =>
    chooseOzonePlatform({ platform: "linux", env, argv });

  it("picks Wayland on a native Wayland session", () => {
    expect(linux({ WAYLAND_DISPLAY: "wayland-0", XDG_SESSION_TYPE: "wayland" })).toBe("wayland");
  });
  it("picks Wayland on WSLg", () => {
    expect(linux({ WSL_DISTRO_NAME: "Ubuntu", WAYLAND_DISPLAY: "wayland-0", DISPLAY: ":0" })).toBe(
      "wayland",
    );
  });
  it("stays on X11 for an X session or an SSH-forwarded display", () => {
    expect(linux({ DISPLAY: ":0", XDG_SESSION_TYPE: "x11" })).toBeNull();
    expect(linux({ DISPLAY: "localhost:10.0", SSH_CONNECTION: "1 2 3 4" })).toBeNull();
    expect(linux({ WAYLAND_DISPLAY: "wayland-0", XDG_SESSION_TYPE: "x11" })).toBeNull();
  });
  it("leaves an explicit choice alone", () => {
    expect(linux({ WAYLAND_DISPLAY: "w" }, ["--ozone-platform=x11"])).toBeNull();
    expect(linux({ WAYLAND_DISPLAY: "w", ELECTRON_OZONE_PLATFORM_HINT: "x11" })).toBeNull();
  });
  it("does nothing off Linux", () => {
    expect(
      chooseOzonePlatform({ platform: "darwin", env: { WAYLAND_DISPLAY: "w" }, argv: [] }),
    ).toBeNull();
  });
});
