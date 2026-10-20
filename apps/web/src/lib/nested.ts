// Read and write dotted keys (`display.bell`) in a nested config object without mutating it.

import type { ConfigObject, ConfigValue } from "./restTypes.ts";

function isObject(value: ConfigValue | undefined): value is ConfigObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function getPath(root: ConfigObject, key: string): ConfigValue | undefined {
  let node: ConfigValue | undefined = root;
  for (const part of key.split(".")) {
    if (!isObject(node)) return undefined;
    node = node[part];
  }
  return node;
}

export function setPath(root: ConfigObject, key: string, value: ConfigValue): ConfigObject {
  const [head, ...rest] = key.split(".");
  if (head === undefined) return root;
  if (rest.length === 0) return { ...root, [head]: value };
  const child = root[head];
  return { ...root, [head]: setPath(isObject(child) ? child : {}, rest.join("."), value) };
}
