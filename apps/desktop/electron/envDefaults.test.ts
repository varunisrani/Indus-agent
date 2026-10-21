import { describe, expect, it } from "vitest";
import {
  applyEnvDefaults,
  EnvDefaultsError,
  parseBakedEnvDefaults,
  validateEnvDefaults,
} from "./envDefaults.ts";

describe("env defaults precedence", () => {
  const table: Array<[string, string | undefined, string | null, string]> = [
    // [case, runtime value, default, result]
    ["absent key takes the default", undefined, "/opt/indus-home", "/opt/indus-home"],
    ["explicit runtime value wins", "/home/me/.indus", "/opt/indus-home", "/home/me/.indus"],
    ["empty runtime value counts as explicit", "", "/opt/indus-home", ""],
    ["null default clears an inherited value", "/inherited", null, ""],
    ["null default clears an absent value to empty", undefined, null, ""],
  ];
  for (const [name, runtime, fallback, want] of table) {
    it(name, () => {
      const env: Record<string, string | undefined> =
        runtime === undefined ? {} : { INDUS_HOME: runtime };
      applyEnvDefaults(env, { INDUS_HOME: fallback });
      expect(env.INDUS_HOME).toBe(want);
    });
  }

  it("reports the keys it changed", () => {
    const env: Record<string, string | undefined> = { INDUS_HOME: "/x" };
    expect(
      applyEnvDefaults(env, { INDUS_HOME: "/y", INDUS_DESKTOP_USER_DATA_DIR: "/data" }),
    ).toEqual(["INDUS_DESKTOP_USER_DATA_DIR"]);
  });

  it("ignores keys outside the allow-list at run time", () => {
    const env: Record<string, string | undefined> = {};
    applyEnvDefaults(env, { PATH: "/evil" } as Record<string, string>);
    expect(env.PATH).toBeUndefined();
  });
});

describe("validation", () => {
  it("accepts allow-listed string or null values", () => {
    expect(validateEnvDefaults({ INDUS_HOME: "/h", INDUS_DESKTOP_USER_DATA_DIR: null })).toEqual({
      INDUS_HOME: "/h",
      INDUS_DESKTOP_USER_DATA_DIR: null,
    });
  });

  it.each([
    [[]],
    [null],
    ["text"],
    [{ OPENAI_API_KEY: "x" }],
    [{ INDUS_HOME: 3 }],
    [{ INDUS_HOME: "a\0b" }],
  ])("refuses %j", (value) => {
    expect(() => validateEnvDefaults(value)).toThrow(EnvDefaultsError);
  });

  it("a bad baked table is ignored at run time, never fatal", () => {
    expect(parseBakedEnvDefaults("{not json")).toEqual({});
    expect(parseBakedEnvDefaults('{"PATH":"/x"}')).toEqual({});
    expect(parseBakedEnvDefaults(undefined)).toEqual({});
    expect(parseBakedEnvDefaults('{"INDUS_HOME":"/h"}')).toEqual({ INDUS_HOME: "/h" });
  });
});
