// One user guide as React elements (W58-J2-1): headings carry our docs site's anchors, a link to
// another guide opens it in this page, a web address opens in a new tab, anything else is text.
// Nothing is ever rendered as HTML; colours come from the theme tokens, so it follows light/dark.

import { ExternalLink } from "lucide-react";
import { Fragment, type ReactNode } from "react";
import { cx } from "../../ui/cx.ts";
import { resolveLink } from "./library.ts";
import {
  type Block,
  type Inline,
  parseBlocks,
  parseInline,
  plainText,
  slugify,
  splitItem,
} from "./markdown.ts";

export interface DocLinkTarget {
  slug: string;
  anchor: string;
}

interface Context {
  slug: string;
  onNavigate: (target: DocLinkTarget) => void;
  /** Anchors already used on this page (repeats get `_1`, `_2` like the docs site). */
  anchors: Map<string, number>;
}

function anchorFor(ctx: Context, text: string): string {
  const base = slugify(plainText(parseInline(text))) || "section";
  const seen = ctx.anchors.get(base) ?? 0;
  ctx.anchors.set(base, seen + 1);
  return seen === 0 ? base : `${base}_${seen}`;
}

function Inlines({ runs, ctx }: { runs: readonly Inline[]; ctx: Context }) {
  return (
    <>
      {runs.map((run, index) => {
        const key = `${run.type}-${index}`;
        switch (run.type) {
          case "text":
            return <Fragment key={key}>{run.text}</Fragment>;
          case "break":
            return <br key={key} />;
          case "code":
            return (
              <code
                key={key}
                className="type-data bg-wash px-1 py-px text-[0.8125rem] break-words text-ink"
              >
                {run.text}
              </code>
            );
          case "strong":
            return (
              <strong key={key} className="font-semibold text-ink">
                <Inlines runs={run.children} ctx={ctx} />
              </strong>
            );
          case "em":
            return (
              <em key={key}>
                <Inlines runs={run.children} ctx={ctx} />
              </em>
            );
          case "strike":
            return (
              <s key={key}>
                <Inlines runs={run.children} ctx={ctx} />
              </s>
            );
          case "link":
            return <DocLink key={key} run={run} ctx={ctx} />;
        }
        return null;
      })}
    </>
  );
}

const LINK = "text-ink underline decoration-line-strong underline-offset-2 hover:decoration-ink";

function DocLink({ run, ctx }: { run: Extract<Inline, { type: "link" }>; ctx: Context }) {
  const target = resolveLink(ctx.slug, run.href);
  const label = <Inlines runs={run.children} ctx={ctx} />;
  if (target === null) return <span>{label}</span>;
  if ("external" in target) {
    return (
      <a href={target.external} target="_blank" rel="noopener noreferrer" className={LINK}>
        {label}
        <ExternalLink aria-hidden="true" className="ml-0.5 inline size-3 align-baseline" />
      </a>
    );
  }
  const query = new URLSearchParams({ page: target.slug });
  return (
    <a
      href={`?${query.toString()}${target.anchor ? `#${target.anchor}` : ""}`}
      className={LINK}
      onClick={(event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
        event.preventDefault();
        ctx.onNavigate(target);
      }}
    >
      {label}
    </a>
  );
}

function inline(text: string, ctx: Context): ReactNode {
  return <Inlines runs={parseInline(text)} ctx={ctx} />;
}

const HEADING_CLASS: Record<number, string> = {
  1: "type-section-title mt-0 mb-4 text-[1.375rem] normal-case tracking-[0.02em]",
  2: "type-section-title mt-8 mb-3 border-b border-line pb-1.5 text-[1.0625rem] normal-case",
  3: "mt-6 mb-2 text-[0.9375rem] font-semibold",
  4: "mt-5 mb-2 text-[0.875rem] font-semibold",
  5: "mt-4 mb-1 text-[0.8125rem] font-semibold",
  6: "mt-4 mb-1 text-[0.8125rem] font-semibold text-ink-muted",
};

/** A list item: its leading text inline, then any blocks it holds (a nested list, code). */
function ListItem({ text, ctx }: { text: string; ctx: Context }) {
  const { lead, rest } = splitItem(text);
  return (
    <li className="my-1">
      {inline(lead, ctx)}
      {rest.trim() ? <Blocks blocks={parseBlocks(rest)} ctx={ctx} /> : null}
    </li>
  );
}

function Blocks({ blocks, ctx }: { blocks: readonly Block[]; ctx: Context }) {
  return (
    <>
      {blocks.map((block, index) => {
        const key = `${block.type}-${index}`;
        switch (block.type) {
          case "paragraph":
            return (
              <p key={key} className="my-3 leading-[1.65]">
                {inline(block.text, ctx)}
              </p>
            );
          case "heading": {
            const level = Math.min(6, Math.max(1, block.level));
            const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
            return (
              <Tag
                key={key}
                id={anchorFor(ctx, block.text)}
                className={cx("scroll-mt-4 text-ink", HEADING_CLASS[level])}
              >
                {inline(block.text, ctx)}
              </Tag>
            );
          }
          case "code":
            return (
              <pre
                key={key}
                className="type-data my-3 overflow-x-auto border border-line bg-wash px-3 py-2.5 text-[0.8125rem] leading-[1.55] text-ink"
              >
                <code>{block.code}</code>
              </pre>
            );
          case "list": {
            const items = block.items.map((item, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: list items have no identity of their own
              <ListItem key={i} text={item} ctx={ctx} />
            ));
            return block.ordered ? (
              <ol key={key} start={block.start} className="my-3 list-decimal pl-6">
                {items}
              </ol>
            ) : (
              <ul key={key} className="my-3 list-disc pl-6">
                {items}
              </ul>
            );
          }
          case "quote":
            return (
              <blockquote
                key={key}
                className="my-3 border-l-2 border-line-strong pl-4 text-ink-muted"
              >
                <Blocks blocks={parseBlocks(block.text)} ctx={ctx} />
              </blockquote>
            );
          case "rule":
            return <hr key={key} className="my-6 border-0 border-t border-line" />;
          case "table":
            return (
              <div key={key} className="my-4 overflow-x-auto">
                <table className="w-full border-collapse text-left text-[0.8125rem]">
                  <thead>
                    <tr>
                      {block.header.map((cell, i) => (
                        <th
                          // biome-ignore lint/suspicious/noArrayIndexKey: columns are positional
                          key={i}
                          className="border-b border-line-strong px-2 py-1.5 align-bottom font-semibold text-ink"
                        >
                          {inline(cell, ctx)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
                      <tr key={r} className="border-b border-line">
                        {row.map((cell, c) => (
                          // biome-ignore lint/suspicious/noArrayIndexKey: columns are positional
                          <td key={c} className="px-2 py-1.5 align-top">
                            {inline(cell, ctx)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
        return null;
      })}
    </>
  );
}

export function DocArticle({
  slug,
  source,
  onNavigate,
}: {
  slug: string;
  source: string;
  onNavigate: (target: DocLinkTarget) => void;
}) {
  const ctx: Context = { slug, onNavigate, anchors: new Map() };
  return (
    <article className="type-serif max-w-[52rem] text-[0.9375rem] text-ink">
      <Blocks blocks={parseBlocks(source)} ctx={ctx} />
    </article>
  );
}
