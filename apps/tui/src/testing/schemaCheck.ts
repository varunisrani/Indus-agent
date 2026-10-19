// A small JSON-Schema checker for the subset the generated protocol schema uses ($ref, type,
// properties, required, additionalProperties, enum, const, anyOf, oneOf, items, numeric and length
// bounds, pattern). Test-only: fixtures are checked against `packages/protocol` with it.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

type Schema = Record<string, unknown>;

export interface ProtocolSchema {
  $defs: Record<string, Schema>;
}

let cached: ProtocolSchema | null = null;

/** The generated schema of `@indus/protocol`. */
export function protocolSchema(): ProtocolSchema {
  if (cached === null) {
    const require = createRequire(import.meta.url);
    const entry = require.resolve("@indus/protocol");
    const path = entry.replace(/index\.ts$/, "generated/schema.json");
    cached = JSON.parse(readFileSync(path, "utf8")) as ProtocolSchema;
  }
  return cached;
}

function typeOk(value: unknown, type: string): boolean {
  switch (type) {
    case "object":
      return typeof value === "object" && value !== null && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    default:
      return false;
  }
}

/** Every reason `value` does not match `schema` (empty when it matches). */
export function check(
  value: unknown,
  schema: Schema,
  defs: Record<string, Schema>,
  path = "$",
): string[] {
  const errors: string[] = [];
  const ref = schema.$ref;
  if (typeof ref === "string") {
    const name = ref.replace("#/$defs/", "");
    const target = defs[name];
    if (target === undefined) return [`${path}: unknown $ref ${ref}`];
    errors.push(...check(value, target, defs, path));
  }
  const type = schema.type;
  if (typeof type === "string" && !typeOk(value, type))
    return [...errors, `${path}: expected ${type}`];
  if (Array.isArray(type) && !type.some((t) => typeOk(value, String(t)))) {
    return [...errors, `${path}: expected one of ${type.join(", ")}`];
  }
  if ("const" in schema && value !== schema.const)
    errors.push(`${path}: expected ${JSON.stringify(schema.const)}`);
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors.push(`${path}: ${JSON.stringify(value)} not in enum`);
  }
  for (const key of ["anyOf", "oneOf"] as const) {
    const options = schema[key];
    if (!Array.isArray(options)) continue;
    const passing = options.filter(
      (option) => check(value, option as Schema, defs, path).length === 0,
    ).length;
    if (key === "anyOf" ? passing === 0 : passing !== 1) {
      errors.push(`${path}: ${passing} of ${options.length} ${key} branches match`);
    }
  }
  if (typeof value === "string") {
    if (typeof schema.minLength === "number" && value.length < schema.minLength)
      errors.push(`${path}: too short`);
    if (typeof schema.maxLength === "number" && value.length > schema.maxLength)
      errors.push(`${path}: too long`);
    if (typeof schema.pattern === "string" && !new RegExp(schema.pattern, "u").test(value)) {
      errors.push(`${path}: does not match ${schema.pattern}`);
    }
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum)
      errors.push(`${path}: below minimum`);
    if (typeof schema.maximum === "number" && value > schema.maximum)
      errors.push(`${path}: above maximum`);
    if (typeof schema.exclusiveMinimum === "number" && value <= schema.exclusiveMinimum) {
      errors.push(`${path}: not above ${schema.exclusiveMinimum}`);
    }
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems)
      errors.push(`${path}: too few items`);
    if (typeof schema.items === "object" && schema.items !== null) {
      value.forEach((item, i) => {
        errors.push(...check(item, schema.items as Schema, defs, `${path}[${i}]`));
      });
    }
  }
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const body = value as Record<string, unknown>;
    const props = (schema.properties ?? {}) as Record<string, Schema>;
    for (const key of (schema.required ?? []) as string[]) {
      if (!(key in body)) errors.push(`${path}.${key}: required`);
    }
    for (const [key, inner] of Object.entries(body)) {
      const propSchema = props[key];
      if (propSchema !== undefined) {
        errors.push(...check(inner, propSchema, defs, `${path}.${key}`));
      } else if (schema.additionalProperties === false) {
        errors.push(`${path}.${key}: not allowed`);
      } else if (
        typeof schema.additionalProperties === "object" &&
        schema.additionalProperties !== null
      ) {
        errors.push(...check(inner, schema.additionalProperties as Schema, defs, `${path}.${key}`));
      }
    }
  }
  return errors;
}

/** Check `value` against the named `$defs` type of the protocol schema. */
export function checkType(value: unknown, typeName: string): string[] {
  const { $defs } = protocolSchema();
  const schema = $defs[typeName];
  if (schema === undefined) return [`no schema type ${typeName}`];
  return check(value, schema, $defs);
}
