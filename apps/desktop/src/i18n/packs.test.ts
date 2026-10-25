import { describe, expect, it, vi } from "vitest";
import { en } from "./en.ts";
import { makeTranslate } from "./index.tsx";
import { type LocalePack, LocalePacks, layerPacks, localeChain, placeholders } from "./packs.ts";

/**
 * A test-only pseudo-locale: every English message bracketed and its letters swapped for accented
 * look-alikes, with `{placeholders}` left exactly as they are. It never ships.
 */
function pseudo(text: string): string {
  const swap: Record<string, string> = { a: "á", e: "é", i: "í", o: "ó", u: "ú", A: "Á", E: "É" };
  const body = text
    .split(/(\{\w+\})/)
    .map((part) => (/^\{\w+\}$/.test(part) ? part : [...part].map((c) => swap[c] ?? c).join("")))
    .join("");
  return `⟦${body}⟧`;
}

function pseudoPack(keys: readonly string[], origin: LocalePack["origin"] = "plugin"): LocalePack {
  const messages: Record<string, string> = {};
  for (const key of keys) messages[key] = pseudo(en[key as keyof typeof en]);
  return { id: `pseudo-${origin}`, locale: "qps", origin, messages };
}

describe("locale packs", () => {
  it("the pseudo-locale overlays English and falls back per key", () => {
    const keys = ["nav.newChat", "find.count", "common.close"];
    const layered = layerPacks(en, [pseudoPack(keys)], "qps");
    const t = makeTranslate(layered.catalog);
    expect(t("nav.newChat")).toBe("⟦Néw séssíón⟧");
    expect(t("find.count", { current: 2, total: 5 })).toBe("⟦2 óf 5⟧");
    expect(t("common.working")).toBe(en["common.working"]); // not in the pack: English
    expect(layered.translated).toBe(3);
    expect(layered.total).toBe(Object.keys(en).length);
    expect(layered.issues).toEqual([]);
  });

  it("a full pseudo pack covers every key and keeps every placeholder", () => {
    const layered = layerPacks(en, [pseudoPack(Object.keys(en))], "qps");
    expect(layered.translated).toBe(Object.keys(en).length);
    for (const key of Object.keys(en) as (keyof typeof en)[]) {
      expect(placeholders(layered.catalog[key])).toEqual(placeholders(en[key]));
    }
  });

  it("bad entries are reported and stay English", () => {
    const pack: LocalePack = {
      id: "broken",
      locale: "qps",
      origin: "backend",
      messages: {
        "no.such.key": "x",
        "nav.newChat": 42,
        "common.close": "   ",
        "find.count": "{current} sur {all}",
        "nav.settings": "Réglages",
      },
    };
    const layered = layerPacks(en, [pack], "qps");
    expect(layered.issues.map((i) => [i.key, i.reason])).toEqual([
      ["no.such.key", "unknown-key"],
      ["nav.newChat", "not-text"],
      ["common.close", "empty"],
      ["find.count", "placeholders"],
    ]);
    expect(layered.catalog["find.count"]).toBe(en["find.count"]);
    expect(layered.catalog["nav.settings"]).toBe("Réglages");
  });

  it("plugin packs layer over backend packs; the region over the language", () => {
    const backend: LocalePack = {
      id: "b",
      locale: "pt",
      origin: "backend",
      messages: { "nav.settings": "Definições", "common.close": "Fechar" },
    };
    const plugin: LocalePack = {
      id: "p",
      locale: "pt",
      origin: "plugin",
      messages: { "nav.settings": "Ajustes" },
    };
    const region: LocalePack = {
      id: "r",
      locale: "pt-BR",
      origin: "backend",
      messages: { "common.close": "Fechar janela" },
    };
    // registration order must not matter for origin rank
    const layered = layerPacks(en, [plugin, region, backend], "pt-BR");
    expect(layered.catalog["nav.settings"]).toBe("Ajustes");
    expect(layered.catalog["common.close"]).toBe("Fechar janela");
    expect(layerPacks(en, [plugin, region, backend], "pt").catalog["common.close"]).toBe("Fechar");
    expect(layerPacks(en, [plugin, region, backend], "fr").translated).toBe(0);
    expect(localeChain("zh-Hant-TW")).toEqual(["zh", "zh-Hant", "zh-Hant-TW"]);
  });

  it("the registry notifies, replaces by id and refuses bad locales", () => {
    const packs = new LocalePacks();
    const listener = vi.fn();
    packs.subscribe(listener);
    const off = packs.register(pseudoPack(["nav.newChat"]));
    packs.register(pseudoPack(["common.close"]));
    expect(packs.list()).toHaveLength(1);
    expect(packs.catalogFor(en, "qps").catalog["common.close"]).toBe("⟦Clósé⟧");
    off();
    expect(listener).toHaveBeenCalledTimes(2);
    expect(() =>
      packs.register({ id: "x", locale: "../etc", origin: "plugin", messages: {} }),
    ).toThrow(/not a locale/);
  });
});
