import { Box } from "ink";
import { afterEach, describe, expect, it } from "vitest";
import { CompletionList } from "./CompletionList.tsx";
import { type CompletionRow, Composer } from "./composerStore.ts";
import { type InkHarness, renderInk } from "./testSupport.ts";

let harness: InkHarness | null = null;

afterEach(() => {
  harness?.unmount();
  harness = null;
});

function rows(count: number): CompletionRow[] {
  const out: CompletionRow[] = [
    { text: "/agents", display: "/agents", meta: "Start, list, follow and stop background tasks" },
    {
      text: "/approvals",
      display: "/approvals",
      meta: "Show or save how risky commands are approved",
    },
  ];
  for (let i = out.length; i < count; i += 1) {
    out.push({ text: `/cmd${i}`, display: `/cmd${i}`, meta: `command ${i}` });
  }
  return out;
}

async function shown(composer: Composer, height: number): Promise<string[]> {
  harness = await renderInk(
    <Box flexDirection="column" height={height}>
      <CompletionList composer={composer} rows={height} />
    </Box>,
  );
  return harness.frame().split("\n");
}

describe("CompletionList", () => {
  // W47 [d]: the count line was drawn on top of the item rows when the list was longer than its
  // box, so one row was squeezed to nothing and the next one overwrote it, leaving its tail behind.
  it("fits the item rows and the count line inside its rows, with no row drawn over another", async () => {
    const composer = new Composer();
    composer.setCompletion({ items: rows(66), index: 0, replaceFrom: 0 });
    const lines = await shown(composer, 8);
    expect(lines.length).toBeLessThanOrEqual(8);
    expect(lines[0]).toContain("/agents");
    expect(lines[0]).toContain("background tasks");
    expect(lines[1]).toContain("/approvals");
    expect(lines[1]?.trimEnd().endsWith("are approved")).toBe(true);
    expect(lines.at(-1)?.trim()).toBe("1/66");
  });

  it("keeps the highlighted row in the window when it moves past the shown rows", async () => {
    const composer = new Composer();
    composer.setCompletion({ items: rows(66), index: 9, replaceFrom: 0 });
    const lines = await shown(composer, 8);
    expect(lines.length).toBeLessThanOrEqual(8);
    expect(lines.some((line) => line.includes("/cmd9 "))).toBe(true);
    expect(lines.at(-1)?.trim()).toBe("10/66");
  });

  it("uses every row for items when they all fit", async () => {
    const composer = new Composer();
    composer.setCompletion({ items: rows(3), index: 0, replaceFrom: 0 });
    const lines = await shown(composer, 3);
    expect(lines.map((line) => line.trim().split(" ")[0])).toEqual([
      "/agents",
      "/approvals",
      "/cmd2",
    ]);
  });
});
