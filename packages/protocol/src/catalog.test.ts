import { readFileSync } from "node:fs";
import { describe, expect, expectTypeOf, it } from "vitest";
import type { Event, TurnDone } from "./generated/types.ts";
import {
  ERROR_CODES,
  ERROR_GROUPS,
  EVENTS,
  type EventName,
  type EventTypes,
  METHODS,
  type MethodName,
  type MethodTypes,
  PROTOCOL_VERSION,
  SERVER_REQUEST_KINDS,
  type ServerRequestKind,
  type ServerRequestTypes,
} from "./index.ts";

interface Schema {
  $defs: Record<string, unknown>;
}

const schema = JSON.parse(
  readFileSync(new URL("./generated/schema.json", import.meta.url), "utf8"),
) as Schema;
const defs = new Set(Object.keys(schema.$defs));

describe("method catalogue", () => {
  it("names a params and a result type for every method, and both exist in the schema", () => {
    const names = Object.keys(METHODS);
    expect(names.length).toBeGreaterThan(0);
    for (const [method, entry] of Object.entries(METHODS)) {
      expect(defs.has(entry.params), `${method} params ${entry.params}`).toBe(true);
      expect(defs.has(entry.result), `${method} result ${entry.result}`).toBe(true);
    }
  });

  it("declares only errors from the table", () => {
    for (const entry of Object.values(METHODS)) {
      for (const name of entry.errors) {
        expect(Object.hasOwn(ERROR_CODES, name), name).toBe(true);
      }
    }
  });

  it("maps every method name to its params and result types at compile time", () => {
    expectTypeOf<keyof MethodTypes>().toEqualTypeOf<MethodName>();
    expectTypeOf<keyof EventTypes>().toEqualTypeOf<EventName>();
    expectTypeOf<keyof ServerRequestTypes>().toEqualTypeOf<ServerRequestKind>();
  });

  it("has a schema type for every event and server-request kind", () => {
    for (const typeName of Object.values(EVENTS)) {
      expect(defs.has(typeName), typeName).toBe(true);
    }
    for (const entry of Object.values(SERVER_REQUEST_KINDS)) {
      expect(defs.has(entry.request), entry.request).toBe(true);
      expect(defs.has(entry.answer), entry.answer).toBe(true);
    }
  });

  it("carries a positive protocol version", () => {
    expect(PROTOCOL_VERSION).toBeGreaterThan(0);
  });
});

describe("error table", () => {
  it("is frozen at every level", () => {
    expect(Object.isFrozen(ERROR_CODES)).toBe(true);
    for (const row of Object.values(ERROR_CODES)) {
      expect(Object.isFrozen(row)).toBe(true);
    }
    expect(() => {
      (ERROR_CODES as unknown as Record<string, unknown>).NEW_CODE = { code: 1 };
    }).toThrow(TypeError);
  });

  it("gives every code one name and a status matching its group", () => {
    const codes = Object.values(ERROR_CODES).map((row) => row.code);
    expect(new Set(codes).size).toBe(codes.length);
    for (const row of Object.values(ERROR_CODES)) {
      expect(row.status).toBe(ERROR_GROUPS[row.group]);
    }
  });

  it("keeps the JSON-RPC 2.0 codes", () => {
    expect(ERROR_CODES.PARSE_ERROR.code).toBe(-32700);
    expect(ERROR_CODES.INVALID_REQUEST.code).toBe(-32600);
    expect(ERROR_CODES.METHOD_NOT_FOUND.code).toBe(-32601);
    expect(ERROR_CODES.INVALID_PARAMS.code).toBe(-32602);
    expect(ERROR_CODES.INTERNAL_ERROR.code).toBe(-32603);
  });
});

describe("generated types", () => {
  it("narrows events by their type", () => {
    const done: Event = {
      type: "turn.done",
      session_id: "s1",
      seq: 3,
      turn_id: "t1",
      reason: "completed",
    };
    if (done.type === "turn.done") {
      expectTypeOf(done).toEqualTypeOf<TurnDone>();
      expect(done.reason).toBe("completed");
    }
  });
});
