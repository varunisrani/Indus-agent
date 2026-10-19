import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { NOT_A_TERMINAL_LINE } from "./lifecycle.ts";
import { MODE_RESET, MOUSE_MODES, seq, writeModeReset } from "./modes.ts";

const CHILD = fileURLToPath(new URL("./testing/exitChild.ts", import.meta.url));

function runChild(path: string): Promise<{ code: number | null; stdout: string }> {
  return new Promise((resolve) => {
    const child = execFile(
      process.execPath,
      [CHILD, path],
      { timeout: 15_000 },
      (error, stdout) => {
        const code = error === null ? 0 : typeof error.code === "number" ? error.code : null;
        resolve({ code: child.exitCode ?? code, stdout });
      },
    );
  });
}

describe("the reset sequence", () => {
  it("turns off every mode the terminal might have on", () => {
    for (const mode of [...MOUSE_MODES, 1004, 2004, 1049])
      expect(MODE_RESET).toContain(`\u001b[?${mode}l`);
    expect(MOUSE_MODES).toEqual(
      expect.arrayContaining([9, 1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016, 2029]),
    );
    expect(MODE_RESET).toContain(seq.locatorOff);
    expect(MODE_RESET).toContain(seq.kittyPop);
    expect(MODE_RESET).toContain(seq.otherKeysOff);
    expect(MODE_RESET).toContain(seq.sgrReset);
    expect(MODE_RESET.endsWith(seq.cursorShow)).toBe(true);
  });

  it("writes nothing to a stream that is not a terminal", () => {
    const writes: string[] = [];
    expect(writeModeReset({ isTTY: false, write: (t) => writes.push(t) })).toBe(false);
    expect(writes).toEqual([]);
    expect(writeModeReset({ isTTY: true, write: (t) => writes.push(t) })).toBe(true);
    expect(writes).toEqual([MODE_RESET]);
  });

  it("survives a stream that throws", () => {
    expect(
      writeModeReset({
        isTTY: true,
        write: () => {
          throw new Error("EIO");
        },
      }),
    ).toBe(false);
  });
});

describe("every exit path resets the terminal (child process)", () => {
  const paths: [string, number][] = [
    ["exit", 0],
    ["sigterm", 143],
    ["sighup", 129],
    ["uncaught", 1],
    ["rejection", 1],
    ["quit", 0],
    ["ctrl-d", 0],
    ["ctrl-c", 130],
    ["update", 42],
    ["memory", 137],
    ["dead", 1],
  ];
  for (const [path, code] of paths) {
    it(`${path} → exit ${code}, Ink unmounted, then modes reset, then the bridge stopped`, async () => {
      const result = await runChild(path);
      expect(result.code).toBe(code);
      const out = result.stdout;
      expect(out.startsWith("<ready>")).toBe(true);
      const unmount = out.indexOf("<unmount>");
      const reset = out.indexOf(MODE_RESET);
      const bridge = out.indexOf("<bridge-stopped>");
      expect(unmount).toBeGreaterThan(0);
      expect(reset).toBeGreaterThan(unmount);
      expect(bridge).toBeGreaterThan(reset);
      expect(out.split(MODE_RESET)).toHaveLength(2);
      expect(out).not.toContain("<timeout>");
    }, 20_000);
  }

  it("without a TTY on stdin prints one line and exits 0", async () => {
    const result = await runChild("no-tty");
    expect(result.code).toBe(0);
    expect(result.stdout).toBe(`${NOT_A_TERMINAL_LINE}\n`);
  }, 20_000);
});
