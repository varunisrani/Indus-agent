import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Composer } from "./composerStore.ts";
import { editComposer, resolveEditor, splitCommand } from "./externalEditor.ts";

let folder = "";

beforeEach(() => {
  folder = mkdtempSync(join(tmpdir(), "indus-editor-test-"));
});

afterEach(() => {
  rmSync(folder, { recursive: true, force: true });
});

/** A fake editor: records what it was given, writes `output` into the file, exits with `code`. */
function fakeEditor(
  output: string,
  code: number,
): { env: NodeJS.ProcessEnv; seen: string; where: string } {
  const script = join(folder, "fake editor.sh");
  const seen = join(folder, "seen.txt");
  const where = join(folder, "where.txt");
  writeFileSync(
    script,
    [
      'cp "$1" "$SEEN"',
      'printf "%s" "$1" > "$WHERE"',
      'printf "%s" "$OUTPUT" > "$1"',
      `exit ${code}`,
      "",
    ].join("\n"),
  );
  return {
    env: {
      ...process.env,
      EDITOR: `sh '${script}'`,
      VISUAL: "",
      SEEN: seen,
      WHERE: where,
      OUTPUT: output,
    },
    seen,
    where,
  };
}

describe("external editor", () => {
  it("submits the saved text when the editor exits 0, and clears the composer", async () => {
    const composer = new Composer();
    composer.pushContinuation("first line");
    composer.setText("draft");
    const fake = fakeEditor("rewritten prompt\n\n", 0);
    const sent: string[] = [];
    let suspended = 0;
    const result = await editComposer(composer, (text) => sent.push(text), {
      env: fake.env,
      suspend: async (run) => {
        suspended += 1;
        await run();
      },
    });
    expect(result).toEqual({ status: "saved", text: "rewritten prompt" });
    expect(readFileSync(fake.seen, "utf8")).toBe("first line\ndraft");
    expect(sent).toEqual(["rewritten prompt"]);
    expect(suspended).toBe(1);
    expect(composer.hasDraft()).toBe(false);
    expect(existsSync(readFileSync(fake.where, "utf8"))).toBe(false);
  });

  it("leaves the draft untouched when the editor exits 1", async () => {
    const composer = new Composer();
    composer.setText("keep me");
    const fake = fakeEditor("should not be used", 1);
    const sent: string[] = [];
    const result = await editComposer(composer, (text) => sent.push(text), { env: fake.env });
    expect(result).toEqual({ status: "aborted", code: 1 });
    expect(sent).toEqual([]);
    expect(composer.text).toBe("keep me");
    expect(existsSync(readFileSync(fake.where, "utf8"))).toBe(false);
  });

  it("does not submit an emptied file", async () => {
    const composer = new Composer();
    composer.setText("x");
    const fake = fakeEditor("  \n", 0);
    const sent: string[] = [];
    await editComposer(composer, (text) => sent.push(text), { env: fake.env });
    expect(sent).toEqual([]);
    expect(composer.text).toBe("x");
  });

  it("reports an editor that cannot start and keeps the draft", async () => {
    const composer = new Composer();
    composer.setText("still here");
    const result = await editComposer(composer, () => {}, {
      env: { ...process.env, VISUAL: join(folder, "no-such-editor"), EDITOR: "" },
    });
    expect(result.status).toBe("failed");
    expect(composer.text).toBe("still here");
  });
});

describe("editor command", () => {
  it("splits VISUAL/EDITOR like a shell, VISUAL first", () => {
    expect(resolveEditor({ VISUAL: 'code --wait "my dir/x"', EDITOR: "nano" })).toEqual([
      "code",
      "--wait",
      "my dir/x",
    ]);
    expect(resolveEditor({ EDITOR: "emacs -nw" })).toEqual(["emacs", "-nw"]);
    expect(splitCommand(`a 'b c' d\\ e "f\\"g"`)).toEqual(["a", "b c", "d e", 'f"g']);
  });

  it("falls back to editor, nano, vi on PATH, else plain vi; notepad on Windows", () => {
    const onPath = (names: string[]) => (path: string) =>
      names.some((name) => path.endsWith(`/${name}`));
    expect(resolveEditor({ PATH: "/a:/b" }, "linux", onPath(["nano", "vi"]))).toEqual(["/a/nano"]);
    expect(resolveEditor({ PATH: "/a:/b" }, "linux", onPath(["vi", "editor"]))).toEqual([
      "/a/editor",
    ]);
    expect(resolveEditor({ PATH: "/a" }, "linux", () => false)).toEqual(["vi"]);
    expect(resolveEditor({}, "win32")).toEqual(["notepad.exe"]);
  });
});
