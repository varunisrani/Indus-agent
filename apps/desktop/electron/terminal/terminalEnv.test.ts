import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CHILD_ENV_FLOOR, isTerminalBlocked, terminalEnv, terminalLocale } from "./terminalEnv.ts";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const CHILD_ENV = path.join(REPO, "src", "indus", "safety", "child_env.py");

/** The names inside `_FLOOR_GROUPS = { ... }` of the agent's env builder. */
function pythonFloor(): string[] {
  const text = fs.readFileSync(CHILD_ENV, "utf8");
  const start = text.indexOf("_FLOOR_GROUPS");
  const end = text.indexOf("STATIC_BLOCKED", start);
  const block = text.slice(start, end);
  const names = new Set<string>();
  for (const match of block.matchAll(/^\s+"([A-Z0-9_]+)",$/gm)) {
    if (match[1] !== undefined) names.add(match[1]);
  }
  return [...names].sort();
}

describe("terminal env", () => {
  it("the floor equals the agent env builder's (child_env.py)", () => {
    const python = pythonFloor();
    expect(python.length).toBeGreaterThan(50);
    expect([...CHILD_ENV_FLOOR].sort()).toEqual(python);
  });

  it("spawned shell env lacks OPENAI_API_KEY set in the parent (and every other credential name)", () => {
    const fakeKey = ["sk", "test", "not", "real"].join("-");
    const parent = {
      PATH: "/usr/bin:/bin",
      HOME: "/home/me",
      LANG: "en_GB.UTF-8",
      OPENAI_API_KEY: fakeKey,
      openai_api_key: fakeKey,
      ANTHROPIC_API_KEY: fakeKey,
      SOME_NEW_PROVIDER_API_KEY: fakeKey,
      GITHUB_TOKEN: fakeKey,
      TELEGRAM_BOT_TOKEN: fakeKey,
      INDUS_HOME: "/home/me/.indus",
      INDUS_DASHBOARD_SESSION_TOKEN: fakeKey,
      _INDUS_FORCE_X: "1",
      npm_config_prefix: "/opt/npm",
      ELECTRON_RUN_AS_NODE: "1",
      NODE_OPTIONS: "--inspect",
      NO_COLOR: "1",
      EDITOR: "vim",
    };
    const env = terminalEnv(parent, { platform: "linux", appVersion: "1.2.3" });
    expect(Object.values(env)).not.toContain(fakeKey);
    expect(env).toEqual({
      PATH: "/usr/bin:/bin",
      HOME: "/home/me",
      LANG: "en_GB.UTF-8",
      EDITOR: "vim",
      TERM: "xterm-256color",
      COLORTERM: "truecolor",
      TERM_PROGRAM: "Indus",
      TERM_PROGRAM_VERSION: "1.2.3",
      LC_CTYPE: "en_GB.UTF-8",
    });
    expect(isTerminalBlocked("SSH_AUTH_SOCK")).toBe(false);
    expect(isTerminalBlocked("AWS_PROFILE")).toBe(false);
  });

  it("Linux locale fix-up: LANG, else C.UTF-8; macOS UTF-8; an explicit LC_CTYPE wins", () => {
    expect(terminalLocale({}, "linux")).toBe("C.UTF-8");
    expect(terminalLocale({ LANG: "de_DE.UTF-8" }, "linux")).toBe("de_DE.UTF-8");
    expect(terminalLocale({}, "darwin")).toBe("UTF-8");
    expect(terminalLocale({ LC_CTYPE: "en_US.UTF-8" }, "linux")).toBe("en_US.UTF-8");
    expect(terminalEnv({}, { platform: "win32", appVersion: "1" }).LC_CTYPE).toBeUndefined();
  });
});
