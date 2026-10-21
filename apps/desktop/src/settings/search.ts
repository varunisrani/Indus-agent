// Settings search (P20-32): every schema field the backend describes and every hand-built row,
// matched by words, each hit carrying the target of the child page that owns it.

import type { MessageKey, Translate } from "../i18n/index.tsx";
import { MANIFEST, pageForField, type SettingsTarget, type ViewId, viewDef } from "./pages.ts";

/** One field of `GET /api/config/schema`. */
export interface SchemaField {
  readonly type: "boolean" | "enum" | "string" | "integer" | "number" | "list";
  readonly description: string;
  readonly category: string;
  readonly options?: readonly string[];
}

export type SchemaFields = Readonly<Record<string, SchemaField>>;

export interface SearchEntry {
  readonly id: string;
  readonly label: string;
  /** "View › Page" */
  readonly context: string;
  readonly description: string;
  readonly keywords: readonly string[];
  readonly target: SettingsTarget;
}

/** `memory.notes_char_limit` → "Notes char limit". */
export function fieldLabel(key: string): string {
  const last = key.split(".").pop() ?? key;
  const words = last.replace(/[_-]+/g, " ").trim();
  return words === "" ? key : words.charAt(0).toUpperCase() + words.slice(1);
}

function contextOf(t: Translate, view: ViewId, page: string): string {
  const def = viewDef(view);
  const pageDef = def?.pages.find((p) => p.id === page);
  if (def === null || pageDef === undefined) return "";
  return `${t(def.label)} › ${t(pageDef.label)}`;
}

/** The searchable entries: schema fields with an owning page, then the hand-built rows. */
export function buildEntries(schema: SchemaFields | null, t: Translate): SearchEntry[] {
  const entries: SearchEntry[] = [];
  for (const [key, field] of Object.entries(schema ?? {})) {
    const owner = pageForField(key);
    if (owner === null) continue;
    entries.push({
      id: `field:${key}`,
      label: fieldLabel(key),
      context: contextOf(t, owner.view.id, owner.page.id),
      description: field.description,
      keywords: [key, ...key.split(/[._]/), field.category, ...(field.options ?? [])],
      target: { view: owner.view.id, page: owner.page.id, field: key, row: null },
    });
  }
  for (const row of MANIFEST) {
    entries.push({
      id: `row:${row.id}`,
      label: t(row.label),
      context: contextOf(t, row.view, row.page),
      description: row.description === undefined ? "" : t(row.description as MessageKey),
      keywords: row.keywords,
      target: { view: row.view, page: row.page, field: null, row: row.id },
    });
  }
  return entries;
}

function fold(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[_.\-/]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** 0 = no match; higher = better (label before context before description and keywords). */
export function score(entry: SearchEntry, query: string): number {
  const needle = fold(query);
  if (needle === "") return 0;
  const label = fold(entry.label);
  const context = fold(entry.context);
  const rest = fold([entry.description, ...entry.keywords].join(" "));
  const all = `${label} ${context} ${rest}`;
  const words = needle.split(" ");
  if (!words.every((word) => all.includes(word))) return 0;
  if (label === needle) return 6;
  if (label.startsWith(needle)) return 5;
  if (words.every((word) => label.includes(word))) return 4;
  if (words.every((word) => context.includes(word) || label.includes(word))) return 3;
  if (entry.keywords.some((k) => fold(k) === needle)) return 2;
  return 1;
}

/** Matching entries, best first (table order breaks ties). */
export function search(entries: readonly SearchEntry[], query: string, limit = 40): SearchEntry[] {
  return entries
    .map((entry, index) => ({ entry, index, value: score(entry, query) }))
    .filter((hit) => hit.value > 0)
    .sort((a, b) => b.value - a.value || a.index - b.index)
    .slice(0, limit)
    .map((hit) => hit.entry);
}
