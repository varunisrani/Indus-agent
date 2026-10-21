import fs from "node:fs";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import {
  cleanupFakes,
  posix,
  tempDir,
  writeFakeIndus,
  writeScript,
} from "../../testing/fakeIndus.ts";
import { runCommand } from "./process.ts";
import {
  launcherTarget,
  parseVersionOutput,
  type ResolverFs,
  type ResolverOptions,
  resolveBackend,
  unwrapWindowsVenvLauncher,
  versionProbe,
} from "./resolver.ts";

afterAll(cleanupFakes);

/** A checkout with a venv whose `indus` is the fake backend. */
function checkout(root: string, scenario?: string): string {
  fs.mkdirSync(path.join(root, "src", "indus"), { recursive: true });
  fs.writeFileSync(path.join(root, "pyproject.toml"), '[project]\nname = "indus-agent"\n');
  fs.writeFileSync(path.join(root, "src", "indus", "__init__.py"), "");
  writeFakeIndus(path.join(root, ".venv", "bin", "indus"), scenario);
  return root;
}

interface Layout {
  base: string;
  home: string;
  homeDir: string;
  sourceRoot: string;
  devRoot: string;
  command: string;
  options: (over?: Partial<ResolverOptions>) => ResolverOptions;
}

/** Every rung populated with a working fake; tests remove rungs to see the next one win. */
function layout(): Layout {
  const base = tempDir();
  const home = path.join(base, "home", ".indus");
  const homeDir = path.join(base, "home");
  const sourceRoot = checkout(path.join(base, "src-root"));
  const devRoot = checkout(path.join(base, "dev-root"));
  const command = writeFakeIndus(path.join(base, "nix", "bin", "indus"));
  checkout(path.join(home, "indus-agent"));
  const other = checkout(path.join(base, "elsewhere"));
  fs.mkdirSync(path.join(homeDir, ".local", "bin"), { recursive: true });
  fs.symlinkSync(
    path.join(other, ".venv", "bin", "indus"),
    path.join(homeDir, ".local", "bin", "indus"),
  );
  // An `indus` on PATH that must never be adopted.
  const pathDir = path.join(base, "pathbin");
  writeFakeIndus(path.join(pathDir, "indus"));
  const options = (over: Partial<ResolverOptions> = {}): ResolverOptions => ({
    env: {
      INDUS_DESKTOP_INDUS_ROOT: sourceRoot,
      INDUS_DESKTOP_INDUS: command,
      PATH: `${pathDir}:/usr/bin:/bin`,
      INDUS_HOME: home,
    },
    argv: [],
    platform: process.platform,
    packaged: false,
    home,
    homeDir,
    devCheckoutRoot: devRoot,
    appExecPath: process.execPath,
    isWsl: false,
    ...over,
  });
  return { base, home, homeDir, sourceRoot, devRoot, command, options };
}

describe.skipIf(!posix)("backend resolution ladder", () => {
  it("precedence table: each rung wins over every lower one", async () => {
    const l = layout();
    const env = l.options().env;
    const expectRung = async (over: Partial<ResolverOptions>, rung: string | null) => {
      const result = await resolveBackend(l.options(over));
      if (rung === null) expect(result.kind).toBe("bootstrap-needed");
      else {
        expect(result.kind).toBe("found");
        if (result.kind === "found") {
          expect(result.backend.rung).toBe(rung);
          expect(result.version).toBe("0.20.0");
        }
      }
    };
    await expectRung({}, "source-root");
    await expectRung({ env: { ...env, INDUS_DESKTOP_INDUS_ROOT: "" } }, "dev-checkout");
    // packaged apps never use their own checkout
    await expectRung({ env: { ...env, INDUS_DESKTOP_INDUS_ROOT: "" }, packaged: true }, "command");
    const noOverrides = { ...env, INDUS_DESKTOP_INDUS_ROOT: "", INDUS_DESKTOP_INDUS: "" };
    // the managed install has no bootstrap marker and still wins
    expect(fs.existsSync(path.join(l.home, ".indus-bootstrap-complete"))).toBe(false);
    await expectRung({ env: noOverrides, packaged: true }, "managed");
    fs.rmSync(path.join(l.home, "indus-agent"), { recursive: true });
    await expectRung({ env: noOverrides, packaged: true }, "launcher");
    fs.rmSync(path.join(l.homeDir, ".local", "bin", "indus"));
    // only an `indus` on PATH is left: never adopted
    await expectRung({ env: noOverrides, packaged: true }, null);
  });

  it("the sentinel carries install hints", async () => {
    const l = layout();
    const result = await resolveBackend(
      l.options({
        env: { PATH: l.options().env.PATH },
        packaged: true,
        homeDir: path.join(l.base, "nobody"),
        home: path.join(l.base, "empty-home"),
      }),
    );
    expect(result).toEqual({
      kind: "bootstrap-needed",
      hints: {
        installDir: path.join(l.base, "empty-home", "indus-agent"),
        platform: process.platform,
        packaged: true,
      },
    });
  });

  it("a failing or hanging probe skips the rung", async () => {
    const l = layout();
    const lines: string[] = [];
    // rung 1 exits 3; rung 2 hangs past the probe deadline; rung 3 answers
    checkout(l.sourceRoot, "version-probe-fails");
    checkout(l.devRoot, "version-probe-hangs");
    const quick = versionProbe((spec, args, options) =>
      runCommand(spec, args, { ...options, timeoutMs: 400 }),
    );
    const started = Date.now();
    const result = await resolveBackend(
      l.options({ probe: quick, log: (line) => lines.push(line) }),
    );
    expect(result.kind === "found" && result.backend.rung).toBe("command");
    expect(Date.now() - started).toBeLessThan(5_000);
    expect(lines.filter((line) => line.includes("version probe failed"))).toHaveLength(2);
  });

  it("ignore-existing and repair skip the installed rungs, not the explicit ones", async () => {
    const l = layout();
    const noOverrides = {
      ...l.options().env,
      INDUS_DESKTOP_INDUS_ROOT: "",
      INDUS_DESKTOP_INDUS: "",
    };
    for (const over of [
      { env: { ...noOverrides, INDUS_DESKTOP_IGNORE_EXISTING: "1" } },
      { env: noOverrides, argv: ["--ignore-existing"] },
      { env: noOverrides, repair: true },
    ]) {
      const result = await resolveBackend(l.options({ ...over, packaged: true }));
      expect(result.kind).toBe("bootstrap-needed");
    }
    const explicit = await resolveBackend(l.options({ repair: true }));
    expect(explicit.kind === "found" && explicit.backend.rung).toBe("source-root");
  });

  it("refuses the app's own binary and a Windows path under WSL", async () => {
    const l = layout();
    const base = { ...l.options().env, INDUS_DESKTOP_INDUS_ROOT: "" };
    const self = await resolveBackend(
      l.options({ env: base, packaged: true, appExecPath: l.command }),
    );
    expect(self.kind === "found" && self.backend.rung).toBe("managed");
    const wsl = await resolveBackend(
      l.options({
        env: { ...base, INDUS_DESKTOP_INDUS: "C:\\Program Files\\Indus\\indus.exe" },
        packaged: true,
        isWsl: true,
      }),
    );
    expect(wsl.kind === "found" && wsl.backend.rung).toBe("managed");
  });

  it("a bare INDUS_DESKTOP_INDUS name is looked up on PATH (the person named it)", async () => {
    const l = layout();
    const result = await resolveBackend(
      l.options({
        env: { ...l.options().env, INDUS_DESKTOP_INDUS_ROOT: "", INDUS_DESKTOP_INDUS: "indus" },
        packaged: true,
      }),
    );
    expect(result.kind === "found" && result.backend.command).toBe(
      path.join(l.base, "pathbin", "indus"),
    );
  });

  it("a checkout without a venv is skipped", async () => {
    const l = layout();
    fs.rmSync(path.join(l.sourceRoot, ".venv"), { recursive: true });
    const lines: string[] = [];
    const result = await resolveBackend(l.options({ log: (line) => lines.push(line) }));
    expect(result.kind === "found" && result.backend.rung).toBe("dev-checkout");
    expect(lines.some((line) => line.includes("no .venv"))).toBe(true);
  });

  it("a launcher script is read for the program it execs", () => {
    const base = tempDir();
    const program = writeFakeIndus(path.join(base, "inst", ".venv", "bin", "indus"));
    const launcher = writeScript(
      path.join(base, "bin", "indus"),
      `# published by the installer\nexec "${program}" "$@"`,
    );
    expect(launcherTarget(launcher)).toBe(program);
    const broken = writeScript(path.join(base, "bin2", "indus"), "echo nothing");
    expect(launcherTarget(broken)).toBeNull();
    fs.symlinkSync(path.join(base, "missing"), path.join(base, "dangling"));
    expect(launcherTarget(path.join(base, "dangling"))).toBeNull();
  });
});

describe("resolver pieces", () => {
  it("parses the version line", () => {
    expect(parseVersionOutput("indus 0.20.0\n")).toBe("0.20.0");
    expect(parseVersionOutput("Indus Agent 0.0.1\nInstall method: git\n")).toBe("0.0.1");
    expect(parseVersionOutput("indus 1.2.3 (managed)\nmore")).toBeNull();
    expect(parseVersionOutput("indus v1.2.3-rc.1\n")).toBe("1.2.3-rc.1");
    expect(parseVersionOutput("python 3.13\n")).toBeNull();
    expect(parseVersionOutput("")).toBeNull();
  });

  it("unwraps a Windows venv launcher to the venv's interpreter", () => {
    const present = new Set([
      "C:\\u\\inst\\.venv\\pyvenv.cfg",
      "C:\\u\\inst\\.venv\\Scripts\\python.exe",
    ]);
    const files: ResolverFs = {
      isFile: (file) => present.has(file),
      isDir: () => false,
      isSymlink: () => false,
      realpath: (file) => file,
      readHead: () => null,
    };
    expect(unwrapWindowsVenvLauncher("C:\\u\\inst\\.venv\\Scripts\\indus.exe", files)).toEqual({
      command: "C:\\u\\inst\\.venv\\Scripts\\python.exe",
      args: ["-m", "indus"],
    });
    expect(unwrapWindowsVenvLauncher("C:\\tools\\indus.exe", files)).toBeNull();
  });
});
