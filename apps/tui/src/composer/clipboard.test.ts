import { describe, expect, it } from "vitest";
import {
  osc52ReadRequest,
  osc52WriteSequence,
  parseOsc52Reply,
  type ReadRunner,
  readClipboard,
  usableText,
  writeClipboard,
} from "./clipboard.ts";

const b64 = (text: string): string => Buffer.from(text, "utf8").toString("base64");

function runner(answers: Record<string, string | Error>): { run: ReadRunner; asked: string[] } {
  const asked: string[] = [];
  const run: ReadRunner = async (cmd) => {
    asked.push(cmd);
    const answer = answers[cmd] ?? new Error(`${cmd} missing`);
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { run, asked };
}

describe("clipboard order", () => {
  it("reads the native clipboard first locally", async () => {
    const native = runner({ xclip: "from native" });
    const queried: string[] = [];
    const text = await readClipboard({
      env: {},
      platform: "linux",
      read: native.run,
      query: async (request) => {
        queried.push(request);
        return `\u001b]52;c;${b64("from terminal")}\u0007`;
      },
    });
    expect(text).toBe("from native");
    expect(queried).toEqual([]);
  });

  it("asks the terminal first over SSH and falls back to native", async () => {
    const native = runner({ xclip: "from native" });
    const remote = { SSH_CONNECTION: "10.0.0.1 22 10.0.0.2 51000" };
    expect(
      await readClipboard({
        env: remote,
        platform: "linux",
        read: native.run,
        query: async () => `c;${b64("osc")}`,
      }),
    ).toBe("osc");
    expect(native.asked).toEqual([]);
    expect(
      await readClipboard({
        env: remote,
        platform: "linux",
        read: native.run,
        query: async () => null,
      }),
    ).toBe("from native");
  });

  it("falls back to the terminal when no native tool works, and tries tools in order", async () => {
    const none = runner({});
    const text = await readClipboard({
      env: { WAYLAND_DISPLAY: "wayland-0" },
      platform: "linux",
      read: none.run,
      query: async () => `c;${b64("terminal text")}`,
    });
    expect(text).toBe("terminal text");
    expect(none.asked).toEqual(["wl-paste", "xclip", "xsel"]);
  });

  it("decodes the PowerShell base64 answer on Windows", async () => {
    const win = runner({ powershell: `${b64("ünïcode")}\r\n` });
    expect(await readClipboard({ env: {}, platform: "win32", read: win.run })).toBe("ünïcode");
  });

  it("returns null when neither source has usable text", async () => {
    const junk = runner({ pbpaste: "\u0000\u0001\u0002" });
    expect(await readClipboard({ env: {}, platform: "darwin", read: junk.run })).toBeNull();
  });
});

describe("usable text", () => {
  it("rejects blank, NUL-bearing and control-heavy text", () => {
    expect(usableText("hello\nworld\t!")).toBe(true);
    expect(usableText("   \n")).toBe(false);
    expect(usableText("a\u0000b")).toBe(false);
    expect(usableText("\u0001\u0002\u0003abc")).toBe(false);
    expect(usableText(null)).toBe(false);
  });
});

describe("OSC 52", () => {
  it("builds the request, wrapped for tmux and screen", () => {
    expect(osc52ReadRequest({})).toBe("\u001b]52;c;?\u0007");
    expect(osc52ReadRequest({ TMUX: "/tmp/t,1,0" })).toBe(
      "\u001bPtmux;\u001b\u001b]52;c;?\u0007\u001b\\",
    );
    expect(osc52ReadRequest({ STY: "1.pts" })).toBe("\u001bP\u001b]52;c;?\u0007\u001b\\");
    expect(osc52WriteSequence("hi", {})).toBe(`\u001b]52;c;${b64("hi")}\u0007`);
  });

  it("parses replies with either terminator and refuses empty or query replies", () => {
    expect(parseOsc52Reply(`\u001b]52;c;${b64("π text")}\u001b\\`)).toBe("π text");
    expect(parseOsc52Reply(`c;${b64("plain")}`)).toBe("plain");
    expect(parseOsc52Reply("c;?")).toBeNull();
    expect(parseOsc52Reply("c;")).toBeNull();
    expect(parseOsc52Reply("nonsense")).toBeNull();
  });
});

describe("writing", () => {
  it("uses a native tool, else the OSC 52 sequence", async () => {
    const wrote: string[] = [];
    expect(
      await writeClipboard("copy me", {
        env: {},
        platform: "linux",
        write: async (cmd) => {
          wrote.push(cmd);
          return cmd === "xsel";
        },
      }),
    ).toBe(true);
    expect(wrote).toEqual(["xclip", "xsel"]);
    const emitted: string[] = [];
    expect(
      await writeClipboard("copy me", {
        env: {},
        platform: "darwin",
        write: async () => false,
        emit: (sequence) => emitted.push(sequence),
      }),
    ).toBe(true);
    expect(emitted).toEqual([osc52WriteSequence("copy me", {})]);
    expect(
      await writeClipboard("x", { env: {}, platform: "darwin", write: async () => false }),
    ).toBe(false);
  });
});

describe("windows", () => {
  it("passes the text base64 in PowerShell's arguments, not on stdin", async () => {
    const seen: { cmd: string; args: readonly string[]; input: string }[] = [];
    await writeClipboard("ünï", {
      env: {},
      platform: "win32",
      write: async (cmd, args, input) => {
        seen.push({ cmd, args, input });
        return true;
      },
    });
    expect(seen).toHaveLength(1);
    expect(seen[0]?.cmd).toBe("powershell");
    expect(seen[0]?.input).toBe("");
    expect(seen[0]?.args.join(" ")).toContain(b64("ünï"));
  });
});
