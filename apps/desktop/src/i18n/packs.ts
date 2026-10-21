// Runtime locale packs (P24-33). Only English ships (D-16.51); this is the mechanism that lets a
// backend or a plugin bring another language later, layered over English key by key:
//
// - a pack names its locale (`fr`, `pt-BR`), its origin (`backend` or `plugin`) and a flat record of
//   messages keyed like `en.ts`;
// - for a locale, the packs that apply are the language's (`pt`) then the region's (`pt-BR`), backend
//   packs before plugin packs within each, in registration order — later wins;
// - an entry is used only when its key exists in English, its value is non-empty text and it has
//   exactly English's `{placeholders}`; anything else is reported and the key stays English, so
//   every key always has a value.

import { type Catalog, type MessageKey, messageKeys } from "./index.tsx";

export type PackOrigin = "backend" | "plugin";

export interface LocalePack {
  readonly id: string;
  readonly locale: string;
  readonly origin: PackOrigin;
  readonly messages: Readonly<Record<string, unknown>>;
}

export type PackIssueReason = "unknown-key" | "not-text" | "empty" | "placeholders";

export interface PackIssue {
  readonly pack: string;
  readonly key: string;
  readonly reason: PackIssueReason;
}

export interface LayeredCatalog {
  readonly catalog: Catalog;
  readonly issues: readonly PackIssue[];
  /** Keys a pack supplied (the rest are English). */
  readonly translated: number;
  readonly total: number;
}

const LOCALE = /^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;
const PLACEHOLDER = /\{(\w+)\}/g;

export function isLocale(value: string): boolean {
  return LOCALE.test(value);
}

/** The `{name}` placeholders of a message, sorted and unique. */
export function placeholders(text: string): string[] {
  return [...new Set([...text.matchAll(PLACEHOLDER)].map((m) => m[1] ?? ""))].sort();
}

/** `pt-BR` → `["pt", "pt-BR"]`: the chain of locales a request falls back through, widest first. */
export function localeChain(locale: string): string[] {
  const parts = locale.split("-");
  return parts.map((_, i) => parts.slice(0, i + 1).join("-"));
}

const ORIGIN_RANK: Record<PackOrigin, number> = { backend: 0, plugin: 1 };

/** English with every applicable pack laid over it (see the module notes). */
export function layerPacks(
  base: Catalog,
  packs: readonly LocalePack[],
  locale: string,
): LayeredCatalog {
  const chain = localeChain(locale);
  const ordered = packs
    .map((pack, index) => ({ pack, index }))
    .filter(({ pack }) => chain.includes(pack.locale))
    .sort(
      (a, b) =>
        chain.indexOf(a.pack.locale) - chain.indexOf(b.pack.locale) ||
        ORIGIN_RANK[a.pack.origin] - ORIGIN_RANK[b.pack.origin] ||
        a.index - b.index,
    )
    .map(({ pack }) => pack);
  const catalog: Record<string, string> = { ...base };
  const supplied = new Set<string>();
  const issues: PackIssue[] = [];
  const known = new Set<string>(messageKeys);
  for (const pack of ordered) {
    for (const [key, value] of Object.entries(pack.messages)) {
      if (!known.has(key) || !(key in base)) {
        issues.push({ pack: pack.id, key, reason: "unknown-key" });
        continue;
      }
      if (typeof value !== "string") {
        issues.push({ pack: pack.id, key, reason: "not-text" });
        continue;
      }
      if (value.trim() === "") {
        issues.push({ pack: pack.id, key, reason: "empty" });
        continue;
      }
      const english = base[key as MessageKey];
      if (placeholders(value).join(",") !== placeholders(english).join(",")) {
        issues.push({ pack: pack.id, key, reason: "placeholders" });
        continue;
      }
      catalog[key] = value;
      supplied.add(key);
    }
  }
  return {
    catalog: catalog as Catalog,
    issues,
    translated: supplied.size,
    total: Object.keys(base).length,
  };
}

/** The packs a window knows, with change listeners (the I18n provider re-layers on a change). */
export class LocalePacks {
  private packs: LocalePack[] = [];
  private readonly listeners = new Set<() => void>();

  list(): readonly LocalePack[] {
    return this.packs;
  }

  /** Add (or replace, by id) a pack; returns its removal. Packs with a bad locale are refused. */
  register(pack: LocalePack): () => void {
    if (!isLocale(pack.locale)) throw new Error(`not a locale: ${pack.locale}`);
    this.packs = [...this.packs.filter((p) => p.id !== pack.id), pack];
    this.changed();
    return () => {
      const before = this.packs.length;
      this.packs = this.packs.filter((p) => p !== pack);
      if (this.packs.length !== before) this.changed();
    };
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  catalogFor(base: Catalog, locale: string): LayeredCatalog {
    return layerPacks(base, this.packs, locale);
  }

  private changed(): void {
    for (const listener of [...this.listeners]) listener();
  }
}

/** The window's packs (none ship; plugins and the backend may register some). */
export const localePacks = new LocalePacks();
