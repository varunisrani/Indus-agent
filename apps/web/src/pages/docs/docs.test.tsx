import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DocArticle } from "./DocArticle.tsx";
import { bundledSlugs, docsNav, loadDoc, parseNav, resolveLink } from "./library.ts";
import { cells, parseBlocks, parseInline, slugify, splitItem } from "./markdown.ts";

describe("docs markdown", () => {
  it("reads headings, fences, tables with escaped pipes, nested lists and skips comments", () => {
    const blocks = parseBlocks(
      [
        "<!-- generated -->",
        "# Title",
        "",
        "| Key | Values |",
        "|---|---|",
        "| `a.b` | 'x' \\| 'y' |",
        "",
        "- one",
        "  - nested",
        "- two",
        "",
        "```bash",
        "indus --tui",
        "```",
      ].join("\n"),
    );
    expect(blocks.map((b) => b.type)).toEqual(["heading", "table", "list", "code"]);
    expect(blocks[1]).toMatchObject({ rows: [["`a.b`", "'x' \\| 'y'"]] });
    expect(blocks[2]).toMatchObject({ items: ["one\n- nested", "two"] });
    expect(blocks[3]).toMatchObject({ lang: "bash", code: "indus --tui" });
    expect(cells("| a \\| b | c |")).toEqual(["a \\| b", "c"]);
  });

  it("keeps a list item's wrapped text inline and its nested blocks apart", () => {
    expect(splitItem("first line\nwrapped on\n- nested")).toEqual({
      lead: "first line\nwrapped on",
      rest: "- nested",
    });
  });

  it("keeps hrefs and turns soft line breaks into spaces", () => {
    expect(parseInline("see [the `cli`](user-guide/cli.md#one)\nnext")).toEqual([
      { type: "text", text: "see " },
      {
        type: "link",
        href: "user-guide/cli.md#one",
        children: [
          { type: "text", text: "the " },
          { type: "code", text: "cli" },
        ],
      },
      { type: "text", text: " next" },
    ]);
  });

  it("makes anchors the way the docs site does", () => {
    expect(slugify("One-shot runs")).toBe("one-shot-runs");
    expect(slugify("Who may talk to the bot?")).toBe("who-may-talk-to-the-bot");
    expect(slugify("Feishu / Lark")).toBe("feishu-lark");
    expect(slugify("`indus auth` add")).toBe("indus-auth-add");
  });
});

describe("docs library", () => {
  it("resolves relative guide links, anchors and web addresses", () => {
    expect(resolveLink("messaging/teams", "../reference/protocol.md")).toEqual({
      slug: "reference/protocol",
      anchor: "",
    });
    expect(resolveLink("index", "user-guide/cli.md#one-shot-runs")).toEqual({
      slug: "user-guide/cli",
      anchor: "one-shot-runs",
    });
    expect(resolveLink("user-guide/cli", "#flags")).toEqual({
      slug: "user-guide/cli",
      anchor: "flags",
    });
    expect(resolveLink("index", "https://example.com/x")).toEqual({
      external: "https://example.com/x",
    });
    expect(resolveLink("index", "javascript:alert(1)")).toBeNull();
    expect(resolveLink("index", "/etc/passwd")).toBeNull();
    expect(resolveLink("index", "../../outside.md")).toBeNull();
  });

  it("reads the site's nav: top-level pages first, then one section per group", () => {
    const nav = parseNav(
      "site_name: x\nnav:\n  - Home: index.md\n  - Guide:\n      - CLI: user-guide/cli.md\n" +
        "      - TUI: user-guide/tui.md\nplugins:\n  - search\n",
    );
    expect(nav).toEqual([
      { title: "", pages: [{ slug: "index", title: "Home" }] },
      {
        title: "Guide",
        pages: [
          { slug: "user-guide/cli", title: "CLI" },
          { slug: "user-guide/tui", title: "TUI" },
        ],
      },
    ]);
  });

  it("bundles every guide and lists each one in the navigation", () => {
    const listed = docsNav().flatMap((section) => section.pages.map((page) => page.slug));
    expect(bundledSlugs().length).toBeGreaterThan(50);
    expect([...listed].sort()).toEqual(bundledSlugs());
  });

  it("every link between guides lands on a bundled guide", async () => {
    const bundled = new Set(bundledSlugs());
    const broken: string[] = [];
    for (const slug of bundled) {
      const text = (await loadDoc(slug)) ?? "";
      for (const match of text.matchAll(/\]\(([^)\s]+)\)/g)) {
        const target = resolveLink(slug, match[1] ?? "");
        if (target && "slug" in target && !bundled.has(target.slug)) {
          broken.push(`${slug} -> ${match[1]}`);
        }
      }
    }
    expect(broken).toEqual([]);
  });
});

describe("doc article", () => {
  it("renders text only, numbers repeated anchors and routes guide links to the page", () => {
    const onNavigate = vi.fn();
    render(
      <DocArticle
        slug="user-guide/cli"
        source={
          "# CLI\n\n## Flags\n\n## Flags\n\n<b>not bold</b> and [tui](tui.md#keys) and " +
          "[site](https://example.com)\n"
        }
        onNavigate={onNavigate}
      />,
    );
    const headings = screen.getAllByRole("heading", { level: 2 });
    expect(headings.map((h) => h.id)).toEqual(["flags", "flags_1"]);
    expect(screen.getByText(/<b>not bold<\/b>/)).toBeTruthy();
    expect(document.querySelector("b")).toBeNull();
    screen.getByRole("link", { name: "tui" }).click();
    expect(onNavigate).toHaveBeenCalledWith({ slug: "user-guide/tui", anchor: "keys" });
    const site = screen.getByRole("link", { name: "site" });
    expect(site.getAttribute("target")).toBe("_blank");
    expect(site.getAttribute("rel")).toBe("noopener noreferrer");
  });
});
