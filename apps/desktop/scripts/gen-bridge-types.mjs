// Generate apps/desktop/src/global.d.ts — the renderer's type for `window.indusDesktop` — from the
// channel table (electron/ipc/channels.ts) and its zod schemas (electron/ipc/schemas.ts), through
// JSON Schema. The output is formatted with the workspace's Biome so `biome format` and the stale
// check agree.
//
//   node scripts/gen-bridge-types.mjs           write src/global.d.ts
//   node scripts/gen-bridge-types.mjs --check   exit 1 when the committed file is stale

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { z } from "zod";
import { CHANNELS } from "../electron/ipc/channels.ts";
import * as schemaModule from "../electron/ipc/schemas.ts";

const { SCHEMAS } = schemaModule;

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const OUTPUT = path.join(APP_DIR, "src", "global.d.ts");

const IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/;

function key(name) {
  return IDENT.test(name) ? name : JSON.stringify(name);
}

function pascal(text) {
  return text.replace(/(^|[-_:])([a-z])/g, (_m, _sep, ch) => ch.toUpperCase());
}

/** A TypeScript type for one JSON Schema node (the subset zod emits). */
export function tsType(schema) {
  if (schema === true || schema === undefined) return "unknown";
  if (schema === false) return "never";
  if (typeof schema.$ref === "string") return schema.$ref.replace(/^#\/\$defs\//, "");
  if ("const" in schema) return JSON.stringify(schema.const);
  if (Array.isArray(schema.enum)) return schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  const alternatives = schema.anyOf ?? schema.oneOf;
  if (Array.isArray(alternatives)) {
    return [...new Set(alternatives.map((s) => wrapUnionMember(tsType(s))))].join(" | ");
  }
  if (Array.isArray(schema.allOf))
    return schema.allOf.map((s) => wrapUnionMember(tsType(s))).join(" & ");
  if (Array.isArray(schema.type)) {
    return schema.type.map((type) => tsType({ ...schema, type })).join(" | ");
  }
  switch (schema.type) {
    case "string":
      return "string";
    case "number":
    case "integer":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "array":
      if (Array.isArray(schema.prefixItems))
        return `[${schema.prefixItems.map(tsType).join(", ")}]`;
      return `Array<${tsType(schema.items)}>`;
    case "object":
      return objectType(schema);
    default:
      return "unknown";
  }
}

function wrapUnionMember(text) {
  return text.includes("=>") ? `(${text})` : text;
}

function objectType(schema) {
  const props = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const fields = Object.entries(props).map(
    ([name, value]) => `${key(name)}${required.has(name) ? "" : "?"}: ${tsType(value)};`,
  );
  const extra = schema.additionalProperties;
  if (extra !== undefined && extra !== false && fields.length === 0) {
    return `Record<${schema.propertyNames ? tsType(schema.propertyNames) : "string"}, ${tsType(extra)}>`;
  }
  if (extra !== undefined && extra !== false) fields.push(`[key: string]: ${tsType(extra)};`);
  if (fields.length === 0) return "Record<string, never>";
  return `{ ${fields.join(" ")} }`;
}

// Every zod schema `schemas.ts` exports by name becomes a named type (`IndusBootProgress`) that
// the members refer to, instead of being repeated inline.
const registry = z.registry();
for (const [name, value] of Object.entries(schemaModule)) {
  if (name !== "SCHEMAS" && value instanceof z.ZodType)
    registry.add(value, { id: `Indus${pascal(name)}` });
}
const namedTypes = new Map();

function jsonSchema(schema, io) {
  const out = z.toJSONSchema(schema, {
    io,
    metadata: registry,
    unrepresentable: "any",
    target: "draft-2020-12",
  });
  for (const [id, def] of Object.entries(out.$defs ?? {})) {
    const text = tsType(def);
    const seen = namedTypes.get(id);
    if (seen !== undefined && seen !== text) {
      throw new Error(
        `${id} reads differently as input and output; give the request its own schema`,
      );
    }
    namedTypes.set(id, text);
  }
  return out;
}

/** Does a request schema accept `undefined` (so the argument may be left out)? */
function optionalArgument(schema) {
  return schema.safeParse(undefined).success;
}

function memberFor(def) {
  const schemas = SCHEMAS[def.name];
  if (def.kind === "invoke") {
    const request = schemas.request;
    const arg = optionalArgument(request) ? "payload?" : "payload";
    return `/** \`${def.name}\` (${def.owner}) */\n${key(def.method)}(${arg}: ${tsType(jsonSchema(request, "input"))}): Promise<${tsType(jsonSchema(schemas.response, "output"))}>;`;
  }
  if (def.kind === "send") {
    const request = schemas.request;
    const arg = optionalArgument(request) ? "payload?" : "payload";
    return `/** \`${def.name}\` (${def.owner}) */\n${key(def.method)}(${arg}: ${tsType(jsonSchema(request, "input"))}): void;`;
  }
  return `/** \`${def.name}\` (${def.owner}) */\n${key(def.method)}(listener: (payload: ${tsType(jsonSchema(schemas.payload, "output"))}) => void): () => void;`;
}

/** The unformatted declaration text. */
export function renderDeclarations() {
  const groups = new Map();
  for (const def of CHANNELS) {
    const entry = groups.get(def.group) ?? { members: [], value: null };
    groups.set(def.group, entry);
    if (def.kind === "sync") entry.value = def;
    else entry.members.push(memberFor(def));
  }
  const body = [];
  const fields = [];
  for (const [group, entry] of groups) {
    const typeName = `IndusDesktop${pascal(group)}`;
    if (entry.value !== null) {
      body.push(`/** \`${entry.value.name}\`: read once when the window loads. */`);
      body.push(
        `export type ${typeName} = Readonly<${tsType(jsonSchema(SCHEMAS[entry.value.name].response, "output"))}>;`,
      );
    } else {
      body.push(`export interface ${typeName} {`, ...entry.members, "}");
    }
    body.push("");
    fields.push(`readonly ${key(group)}: ${typeName};`);
  }
  const named = [...namedTypes.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .flatMap(([id, text]) => [`export type ${id} = ${text};`, ""]);
  const out = [
    "// GENERATED by scripts/gen-bridge-types.mjs from electron/ipc/channels.ts and",
    "// electron/ipc/schemas.ts. Do not edit; run `pnpm --filter @indus/desktop gen:bridge-types`.",
    "",
    "/** Error codes a bridge call can reject with (the message starts with `<code>: `). */",
    'export type IndusBridgeErrorCode = "sender-rejected" | "bad-request" | "not-available" | "internal" | (string & {});',
    "",
    ...named,
    ...body,
  ];
  out.push(`export interface IndusDesktopBridge { ${fields.join(" ")} }`);
  out.push("");
  out.push("declare global { interface Window { readonly indusDesktop: IndusDesktopBridge; } }");
  out.push("");
  return out.join("\n");
}

function repoRoot() {
  let dir = APP_DIR;
  while (!fs.existsSync(path.join(dir, "pnpm-workspace.yaml"))) {
    const up = path.dirname(dir);
    if (up === dir) throw new Error("pnpm-workspace.yaml not found above apps/desktop");
    dir = up;
  }
  return dir;
}

/** Format with the workspace's Biome (stdin mode), exactly as `biome format` would. */
export function formatWithBiome(text) {
  const root = repoRoot();
  const bin = path.join(
    root,
    "node_modules",
    ".bin",
    process.platform === "win32" ? "biome.cmd" : "biome",
  );
  const result = spawnSync(bin, ["format", `--stdin-file-path=${OUTPUT}`], {
    cwd: root,
    input: text,
    encoding: "utf8",
    shell: process.platform === "win32",
  });
  if (result.status !== 0)
    throw new Error(`biome format failed: ${result.stderr || result.stdout}`);
  return result.stdout;
}

export function generate() {
  // Biome can need a second pass to settle on long signatures; format until it is stable.
  let text = formatWithBiome(renderDeclarations());
  for (let pass = 0; pass < 3; pass += 1) {
    const again = formatWithBiome(text);
    if (again === text) break;
    text = again;
  }
  return text;
}

function main(argv) {
  const text = generate();
  if (argv.includes("--check")) {
    const current = fs.existsSync(OUTPUT) ? fs.readFileSync(OUTPUT, "utf8") : "";
    if (current !== text) {
      process.stderr.write("src/global.d.ts is stale: run `node scripts/gen-bridge-types.mjs`\n");
      return 1;
    }
    return 0;
  }
  fs.writeFileSync(OUTPUT, text);
  process.stdout.write(`wrote ${path.relative(process.cwd(), OUTPUT)}\n`);
  return 0;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  process.exitCode = main(process.argv.slice(2));
}
