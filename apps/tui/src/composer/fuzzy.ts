// Ranking for local command lists (P12-18): slash dispatch fallbacks and pickers use it.
// Lower is better. Name fields (id, label, aliases) score 0 exact, 1 prefix, 2 substring;
// description words score 3 exact, 4 prefix, 5 substring; anything else does not match.

export const NO_MATCH = Number.POSITIVE_INFINITY;

export interface FuzzyItem {
  readonly id: string;
  readonly label?: string;
  readonly aliases?: readonly string[];
  readonly description?: string;
}

/** `  /Model ` and `model` are the same query. */
export function normalizeQuery(query: string): string {
  return query.trim().replace(/^\/+/, "").toLowerCase();
}

/** A field as a whole plus each of its alphanumeric words, lowercased. */
function pieces(value: string): string[] {
  const low = value.toLowerCase();
  return [low, ...low.split(/[^\p{L}\p{N}]+/u).filter((word) => word !== "")];
}

/** Best tier (0 exact, 1 prefix, 2 substring) of `query` over `fields`, or NO_MATCH. */
function tier(fields: readonly string[], query: string): number {
  let best = NO_MATCH;
  for (const field of fields) {
    if (field === query) return 0;
    if (field.startsWith(query)) best = Math.min(best, 1);
    else if (field.includes(query)) best = Math.min(best, 2);
  }
  return best;
}

/** Score of an item for an already-normalised query. */
export function fuzzyScore(item: FuzzyItem, query: string): number {
  if (query === "") return 0;
  const names = [item.id, item.label ?? "", ...(item.aliases ?? [])]
    .filter((name) => name !== "")
    .flatMap(pieces);
  const byName = tier(names, query);
  if (byName === 0) return 0;
  const words = item.description ? pieces(item.description) : [];
  const byWord = tier(words, query);
  return Math.min(byName, byWord === NO_MATCH ? NO_MATCH : byWord + 3);
}

/** Matching items, best first; ties keep their input order. An empty query keeps everything. */
export function rankItems<T>(
  items: readonly T[],
  query: string,
  view: (item: T) => FuzzyItem,
): T[] {
  const wanted = normalizeQuery(query);
  if (wanted === "") return [...items];
  return items
    .map((item, order) => ({ item, order, score: fuzzyScore(view(item), wanted) }))
    .filter((entry) => entry.score !== NO_MATCH)
    .sort((a, b) => a.score - b.score || a.order - b.order)
    .map((entry) => entry.item);
}
