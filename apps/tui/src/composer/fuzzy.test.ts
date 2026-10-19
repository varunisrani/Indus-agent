import { describe, expect, it } from "vitest";
import { type FuzzyItem, fuzzyScore, NO_MATCH, normalizeQuery, rankItems } from "./fuzzy.ts";

const branch: FuzzyItem = {
  id: "branch",
  label: "Branch",
  aliases: ["fork"],
  description: "Start a side conversation from this point",
};

describe("fuzzy tiers", () => {
  it.each([
    ["branch", 0],
    ["fork", 0],
    ["bra", 1],
    ["for", 1],
    ["anc", 2],
    ["ork", 2],
    ["side", 3],
    ["conver", 4],
    ["versation", 5],
    ["start a side", 4],
    ["side conversation", 5],
    ["zebra", NO_MATCH],
  ])("%s scores %s", (query, score) => {
    expect(fuzzyScore(branch, query)).toBe(score);
  });

  it("takes the best of name and description tiers", () => {
    expect(fuzzyScore({ id: "sum", description: "summary of the summit" }, "sum")).toBe(0);
    expect(fuzzyScore({ id: "recap", description: "summary of the day" }, "summary")).toBe(3);
  });
});

describe("normalising and ranking", () => {
  it("trims, strips leading slashes and lowercases the query", () => {
    expect(normalizeQuery("  //Model ")).toBe("model");
  });

  it("ranks best first, keeps input order on ties, drops non-matches", () => {
    const items: FuzzyItem[] = [
      { id: "reload", description: "reload the model list" },
      { id: "model", description: "choose a model" },
      { id: "models-refresh" },
      { id: "help" },
    ];
    expect(rankItems(items, "/Model", (item) => item).map((item) => item.id)).toEqual([
      "model",
      "models-refresh",
      "reload",
    ]);
  });

  it("keeps everything, in order, for an empty query", () => {
    const items = [{ id: "b" }, { id: "a" }];
    expect(rankItems(items, "  / ", (item) => item)).toEqual(items);
  });
});
