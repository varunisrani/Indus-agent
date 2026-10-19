import { describe, expect, it } from "vitest";
import {
  declaredOnly,
  IMAGE_ONLY_PROMPT,
  isTruthy,
  launchOptions,
  maxTurnsValue,
  nameList,
  startupPrompt,
  startupResumeId,
  toolProgressMode,
} from "./launchOptions.ts";

describe("launch options from the environment", () => {
  it("maps every launcher variable to a creation option", () => {
    const options = launchOptions({
      INDUS_MODEL: "vendor/model-a",
      INDUS_TUI_PROVIDER: "vendor",
      INDUS_TUI_REASONING: "high",
      INDUS_TUI_CWD: "/work/here",
      INDUS_CWD: "/ignored",
      INDUS_TUI_TOOLSETS: "web, files,,web",
      INDUS_TUI_SKILLS: "review,\nplan, review",
      INDUS_TUI_MAX_TURNS: "12",
      INDUS_TUI_YOLO: "1",
      INDUS_TUI_SAFE_MODE: "Yes",
      INDUS_TUI_CHECKPOINTS: "0",
      INDUS_TUI_PASS_SESSION_ID: "on",
    });
    expect(options).toEqual({
      model: "vendor/model-a",
      provider: "vendor",
      reasoning_effort: "high",
      cwd: "/work/here",
      toolsets: ["web", "files"],
      skills: ["review", "plan"],
      max_turns: 12,
      yolo: true,
      safe_mode: true,
      pass_session_id: true,
    });
  });

  it("keeps a max-turns word as the trimmed word", () => {
    expect(maxTurnsValue(" unlimited ")).toBe("unlimited");
    expect(maxTurnsValue("-1")).toBe(-1);
    expect(maxTurnsValue("5.5")).toBe("5.5");
  });

  it("reads truthy words only", () => {
    for (const word of ["1", "true", "YES", " on "]) expect(isTruthy(word)).toBe(true);
    for (const word of ["0", "false", "no", "", undefined, "y"]) expect(isTruthy(word)).toBe(false);
  });

  it("dedupes name lists keeping the first order", () => {
    expect(nameList("b,a,b", /,/)).toEqual(["b", "a"]);
    expect(nameList(undefined)).toEqual([]);
  });

  it("sends only declared keys and names the rest", () => {
    const { params, dropped } = declaredOnly({ model: "m", skills: ["x"], cwd: "/c", yolo: true });
    expect(params).toEqual({ model: "m", cwd: "/c" });
    expect(dropped).toEqual(["skills", "yolo"]);
  });

  it("accepts only known tool-progress modes", () => {
    expect(toolProgressMode({})).toBeNull();
    expect(toolProgressMode({ INDUS_TUI_TOOL_PROGRESS: "Verbose" })).toBe("verbose");
    expect(toolProgressMode({ INDUS_TUI_TOOL_PROGRESS: "loud" })).toEqual({ invalid: "loud" });
  });

  it("reads the startup prompt and resume id", () => {
    expect(startupPrompt({})).toBeNull();
    expect(startupPrompt({ INDUS_TUI_QUERY: "/help" })).toEqual({ text: "/help", image: null });
    expect(startupPrompt({ INDUS_TUI_IMAGE: "/tmp/a.png" })).toEqual({
      text: IMAGE_ONLY_PROMPT,
      image: "/tmp/a.png",
    });
    expect(startupResumeId({ INDUS_TUI_RESUME: " s-9 " })).toBe("s-9");
    expect(startupResumeId({ INDUS_TUI_RESUME: "" })).toBeNull();
  });
});
