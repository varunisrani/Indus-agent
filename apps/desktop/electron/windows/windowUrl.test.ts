import { describe, expect, it } from "vitest";
import { type AppOrigin, fileAppOrigin } from "../appOrigin.ts";
import { parseWindowQuery, windowUrl } from "./windowUrl.ts";

const DEV: AppOrigin = { kind: "dev", origin: "http://localhost:5173" };
const FILE = fileAppOrigin("/opt/Indus/app/dist/index.html", "linux");

describe("window URLs", () => {
  it("puts the kind in the query before the hash", () => {
    expect(windowUrl(DEV, "main")).toBe("http://localhost:5173/");
    expect(windowUrl(DEV, "secondary", { sessionId: "s 1/x", watch: true, profile: "work" })).toBe(
      "http://localhost:5173/?win=secondary&watch=1&profile=work#/s%201%2Fx",
    );
    expect(windowUrl(FILE, "instance", { profile: "home" })).toBe(
      "file:///opt/Indus/app/dist/index.html?win=instance&profile=home",
    );
    expect(windowUrl(FILE, "quick", { profile: "ignored" })).toBe(
      "file:///opt/Indus/app/dist/index.html?win=quick",
    );
  });

  it("a HUD carries its session in the hash and its profile in the query (P20-35)", () => {
    expect(windowUrl(DEV, "hud", { sessionId: "s-1", profile: "work" })).toBe(
      "http://localhost:5173/?win=hud&profile=work#/s-1",
    );
    expect(windowUrl(DEV, "hud")).toBe("http://localhost:5173/?win=hud");
  });

  it("parses the flags back", () => {
    expect(
      parseWindowQuery("http://localhost:5173/?win=secondary&watch=1&profile=work#/s"),
    ).toEqual({
      kind: "secondary",
      watch: true,
      profile: "work",
    });
    expect(parseWindowQuery("http://localhost:5173/?win=evil")).toEqual({
      kind: "main",
      watch: false,
      profile: null,
    });
    expect(parseWindowQuery("garbage")).toEqual({ kind: "main", watch: false, profile: null });
  });
});
