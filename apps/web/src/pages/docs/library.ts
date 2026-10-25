// Our user guides inside the dashboard (W58-J2-1): the pages of docs/user, bundled at build time
// (one lazy chunk per page, so the Docs page loads only the page it shows), in the order and with
// the titles of our docs site's navigation (the `nav:` of mkdocs.yml). The Docker and Nix web
// builds copy docs/user and mkdocs.yml next to apps/ for this.

import MKDOCS_YML from "../../../../../mkdocs.yml?raw";

const SOURCES = import.meta.glob<string>("../../../../../docs/user/**/*.md", {
  query: "?raw",
  import: "default",
});
const PREFIX = "../../../../../docs/user/";

export interface DocPage {
  /** docs/user-relative path without `.md` (`user-guide/cli`). */
  slug: string;
  title: string;
}

export interface DocSection {
  title: string;
  pages: DocPage[];
}

/** Every bundled page's slug. */
export function bundledSlugs(): string[] {
  return Object.keys(SOURCES)
    .map((path) => path.slice(PREFIX.length).replace(/\.md$/, ""))
    .sort();
}

/** The page's Markdown, or null when no such page is bundled. */
export async function loadDoc(slug: string): Promise<string | null> {
  const load = SOURCES[`${PREFIX}${slug}.md`];
  return load ? await load() : null;
}

const NAV_ITEM = /^(\s*)-\s+(.+?):\s*(\S+\.md)?\s*$/;

/**
 * The `nav:` tree of a mkdocs.yml text: top-level pages land in a leading untitled section, each
 * `- Title:` with nested entries opens a section. Only the plain `- Title: path.md` form our
 * navigation uses is read.
 */
export function parseNav(yml: string): DocSection[] {
  const lines = yml.replace(/\r\n?/g, "\n").split("\n");
  const start = lines.findIndex((line) => /^nav:\s*$/.test(line));
  if (start < 0) return [];
  const sections: DocSection[] = [{ title: "", pages: [] }];
  let current = sections[0] as DocSection;
  let topIndent: number | null = null;
  for (const line of lines.slice(start + 1)) {
    if (line.trim() === "" || line.trim().startsWith("#")) continue;
    if (!/^\s/.test(line)) break; // the next top-level key ends the nav
    const match = NAV_ITEM.exec(line);
    if (!match) continue;
    const indent = match[1]?.length ?? 0;
    const title = (match[2] ?? "").replace(/^["']|["']$/g, "");
    const path = match[3];
    topIndent ??= indent;
    if (indent === topIndent) {
      if (path) {
        (sections[0] as DocSection).pages.push({ slug: path.replace(/\.md$/, ""), title });
      } else {
        current = { title, pages: [] };
        sections.push(current);
      }
    } else if (path) {
      current.pages.push({ slug: path.replace(/\.md$/, ""), title });
    }
  }
  return sections.filter((section) => section.pages.length > 0);
}

/** Our docs navigation, limited to the pages this build bundles. */
export function docsNav(): DocSection[] {
  const bundled = new Set(bundledSlugs());
  return parseNav(MKDOCS_YML)
    .map((section) => ({
      title: section.title,
      pages: section.pages.filter((page) => bundled.has(page.slug)),
    }))
    .filter((section) => section.pages.length > 0);
}

export const HOME_SLUG = "index";

/**
 * Where a link in page `from` points: `{slug, anchor}` for another bundled page (relative `.md`
 * paths, `#anchor` on the same page), `{external}` for an http(s) address, or null for anything
 * else (shown as text).
 */
export function resolveLink(
  from: string,
  href: string,
): { slug: string; anchor: string } | { external: string } | null {
  if (/^https?:\/\//i.test(href)) return { external: href };
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("/")) return null;
  const [pathPart = "", anchor = ""] = href.split("#", 2);
  if (pathPart === "") return { slug: from, anchor };
  if (!pathPart.endsWith(".md")) return null;
  const parts = from.split("/").slice(0, -1);
  for (const segment of pathPart.replace(/\.md$/, "").split("/")) {
    if (segment === "..") {
      if (parts.length === 0) return null;
      parts.pop();
    } else if (segment !== "." && segment !== "") {
      parts.push(segment);
    }
  }
  return { slug: parts.join("/"), anchor };
}
