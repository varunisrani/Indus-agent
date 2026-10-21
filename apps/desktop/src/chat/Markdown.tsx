// Model text as React elements (no raw HTML). Links never navigate: a click asks main's audited
// opener, which only opens http(s) after a real user click (D-16.20).

import { Fragment, type ReactNode } from "react";
import { desktopBridge } from "../app/bridge.ts";
import { useT } from "../i18n/index.tsx";
import { type Inline, parseBlocks, parseInline } from "./markdown.ts";
import { CodeBlock, DiagramBlock, MathBlock } from "./RichBlocks.tsx";

function openLink(url: string): void {
  void desktopBridge()?.external.open({ url });
}

function Inlines(props: { runs: readonly Inline[] }) {
  const t = useT();
  return (
    <>
      {props.runs.map((run, index) => {
        const key = `${run.type}-${index}`;
        switch (run.type) {
          case "text":
            return <Fragment key={key}>{run.text}</Fragment>;
          case "break":
            return <br key={key} />;
          case "code":
            return <code key={key}>{run.text}</code>;
          case "math":
            return <MathBlock key={key} tex={run.tex} display={false} />;
          case "strong":
            return (
              <strong key={key}>
                <Inlines runs={run.children} />
              </strong>
            );
          case "em":
            return (
              <em key={key}>
                <Inlines runs={run.children} />
              </em>
            );
          case "strike":
            return (
              <s key={key}>
                <Inlines runs={run.children} />
              </s>
            );
          case "link":
            return (
              <button
                key={key}
                type="button"
                className="md-link"
                title={t("link.open", { url: run.href })}
                onClick={() => openLink(run.href)}
              >
                {run.text}
              </button>
            );
        }
        return null;
      })}
    </>
  );
}

function inline(text: string): ReactNode {
  return <Inlines runs={parseInline(text)} />;
}

export function Markdown(props: { text: string }) {
  const blocks = parseBlocks(props.text);
  return (
    <div className="md">
      {blocks.map((block, index) => {
        const key = `${block.type}-${index}`;
        switch (block.type) {
          case "paragraph":
            return <p key={key}>{inline(block.text)}</p>;
          case "heading": {
            const level = Math.min(6, Math.max(3, block.level + 2));
            const Tag = `h${level}` as "h3" | "h4" | "h5" | "h6";
            return <Tag key={key}>{inline(block.text)}</Tag>;
          }
          case "code":
            return block.lang === "mermaid" ? (
              <DiagramBlock key={key} source={block.code} closed={block.closed} />
            ) : (
              <CodeBlock key={key} code={block.code} lang={block.lang} closed={block.closed} />
            );
          case "math":
            return <MathBlock key={key} tex={block.tex} display closed={block.closed} />;
          case "list": {
            const items = block.items.map((item, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: list items have no identity of their own
              <li key={i}>{inline(item)}</li>
            ));
            return block.ordered ? (
              <ol key={key} start={block.start}>
                {items}
              </ol>
            ) : (
              <ul key={key}>{items}</ul>
            );
          }
          case "quote":
            return (
              <blockquote key={key}>
                <Markdown text={block.text} />
              </blockquote>
            );
          case "rule":
            return <hr key={key} />;
          case "table":
            return (
              <div key={key} className="md-table">
                <table>
                  <thead>
                    <tr>
                      {block.header.map((cell, i) => (
                        // biome-ignore lint/suspicious/noArrayIndexKey: columns are positional
                        <th key={i}>{inline(cell)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
                      <tr key={r}>
                        {row.map((cell, c) => (
                          // biome-ignore lint/suspicious/noArrayIndexKey: columns are positional
                          <td key={c}>{inline(cell)}</td>
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
    </div>
  );
}
