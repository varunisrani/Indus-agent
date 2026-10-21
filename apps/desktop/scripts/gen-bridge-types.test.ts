import fs from "node:fs";
import { describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { generate, OUTPUT, tsType } from "./gen-bridge-types.mjs";

const toTs = tsType as (schema: unknown) => string;

describe("bridge typings", () => {
  it("src/global.d.ts equals a fresh generation (stale check)", () => {
    const fresh = (generate as () => string)();
    const committed = fs.readFileSync(OUTPUT as string, "utf8");
    expect(committed, "run `pnpm --filter @indus/desktop gen:bridge-types`").toBe(fresh);
  });

  it("maps the JSON Schema subset zod emits", () => {
    expect(toTs({ type: "string" })).toBe("string");
    expect(toTs({ type: "integer" })).toBe("number");
    expect(toTs({ const: 404 })).toBe("404");
    expect(toTs({ enum: ["a", "b"] })).toBe('"a" | "b"');
    expect(toTs({ anyOf: [{ type: "string" }, { type: "null" }] })).toBe("string | null");
    expect(toTs({ type: "array", items: { type: "number" } })).toBe("Array<number>");
    expect(toTs({ type: "object", properties: { a: { type: "string" } }, required: [] })).toBe(
      "{ a?: string; }",
    );
    expect(
      toTs({
        type: "object",
        propertyNames: { type: "string" },
        additionalProperties: { type: "string" },
      }),
    ).toBe("Record<string, string>");
    expect(toTs({ $ref: "#/$defs/IndusZoomState" })).toBe("IndusZoomState");
    expect(toTs({})).toBe("unknown");
  });
});
