// One session's full history, rendered as light Markdown with each tool call folded away. When
// opened from a search hit, the searched words are marked and the first mark scrolled into view.

import { useQuery } from "@tanstack/react-query";
import { Fragment, type ReactNode, useEffect, useRef } from "react";
import { api } from "../../lib/api.ts";
import type { SessionMessage } from "../../lib/restTypes.ts";
import { QueryState } from "../../ui/controls.tsx";

/** Render a server snippet whose matches are wrapped in `<mark>…</mark>`, as text (no HTML). */
export function SnippetText({ snippet }: { snippet: string }) {
  const parts: ReactNode[] = [];
  const pattern = /<mark>(.*?)<\/mark>/gs;
  let last = 0;
  let index = 0;
  for (const match of snippet.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) parts.push(<Fragment key={index++}>{snippet.slice(last, at)}</Fragment>);
    parts.push(<mark key={index++}>{match[1]}</mark>);
    last = at + match[0].length;
  }
  if (last < snippet.length) parts.push(<Fragment key={index++}>{snippet.slice(last)}</Fragment>);
  return <>{parts}</>;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Plain text with every case-insensitive occurrence of `term` marked. */
export function MarkedText({ text, term }: { text: string; term: string }) {
  const words = term.trim().split(/\s+/).filter(Boolean).map(escapeRegExp);
  if (words.length === 0) return <>{text}</>;
  const pattern = new RegExp(`(${words.join("|")})`, "gi");
  const pieces = text.split(pattern);
  return (
    <>
      {pieces.map((piece, i) =>
        i % 2 === 1 ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: pieces are positional and never reorder
          <mark key={i}>{piece}</mark>
        ) : (
          // biome-ignore lint/suspicious/noArrayIndexKey: pieces are positional and never reorder
          <Fragment key={i}>{piece}</Fragment>
        ),
      )}
    </>
  );
}

interface Block {
  kind: "code" | "heading" | "paragraph";
  text: string;
  level?: number;
}

/** Split message text into fenced code, headings and paragraphs (enough Markdown to read by). */
export function markdownBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", text: paragraph.join("\n") });
    paragraph = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    if (line.startsWith("```")) {
      flush();
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !(lines[i] ?? "").startsWith("```")) {
        code.push(lines[i] ?? "");
        i += 1;
      }
      blocks.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      blocks.push({ kind: "heading", text: heading[2] ?? "", level: heading[1]?.length ?? 1 });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    paragraph.push(line);
  }
  flush();
  return blocks;
}

const HEADING_TAGS = ["h3", "h4", "h5", "h6"] as const;

function MarkdownText({ text, term }: { text: string; term: string }) {
  return (
    <div className="flex flex-col gap-2">
      {markdownBlocks(text).map((block, i) => {
        const key = `${block.kind}-${i}`;
        if (block.kind === "code") {
          return (
            <pre key={key} className="overflow-x-auto rounded-md bg-code p-2 font-mono text-xs">
              <MarkedText text={block.text} term={term} />
            </pre>
          );
        }
        if (block.kind === "heading") {
          // Message headings sit below the page (h1) and section (h2) levels.
          const Heading = HEADING_TAGS[Math.min(3, (block.level ?? 1) - 1)] ?? "h6";
          return (
            <Heading key={key} className="text-sm font-semibold">
              <MarkedText text={block.text} term={term} />
            </Heading>
          );
        }
        return (
          <p key={key} className="whitespace-pre-wrap">
            <MarkedText text={block.text} term={term} />
          </p>
        );
      })}
    </div>
  );
}

function MessageView({ message, term }: { message: SessionMessage; term: string }) {
  if (message.role === "tool") {
    return (
      <details className="rounded-md border border-line px-2 py-1 text-xs">
        <summary className="cursor-pointer text-muted">
          Result from {message.tool_name ?? "a tool"}
        </summary>
        <pre className="mt-1 overflow-x-auto font-mono whitespace-pre-wrap">
          <MarkedText text={message.content ?? ""} term={term} />
        </pre>
      </details>
    );
  }
  return (
    <article className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted uppercase">{message.role}</span>
      {message.content ? <MarkdownText text={message.content} term={term} /> : null}
      {(message.tool_calls ?? []).map((call) => (
        <details key={call.id} className="rounded-md border border-line px-2 py-1 text-xs">
          <summary className="cursor-pointer text-muted">Tool call: {call.name}</summary>
          <pre className="mt-1 overflow-x-auto font-mono whitespace-pre-wrap">{call.arguments}</pre>
        </details>
      ))}
    </article>
  );
}

export function SessionDetail({
  sessionId,
  highlight = "",
}: {
  sessionId: string;
  highlight?: string;
}) {
  const messages = useQuery({
    queryKey: ["sessions", "messages", sessionId],
    queryFn: () => api.sessions.messages(sessionId),
  });
  const root = useRef<HTMLDivElement>(null);
  const list = messages.data?.messages ?? [];

  useEffect(() => {
    if (!highlight || list.length === 0) return;
    const first = root.current?.querySelector("mark");
    if (first && typeof first.scrollIntoView === "function") {
      first.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [highlight, list]);

  return (
    <div
      ref={root}
      className="flex flex-col gap-3 border-t border-line pt-3"
      data-testid={`detail-${sessionId}`}
    >
      <QueryState
        loading={messages.isPending}
        error={messages.error}
        empty={list.length === 0}
        emptyText="This session has no messages."
      >
        {list.map((message, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: history rows are append-only and positional
          <MessageView key={i} message={message} term={highlight} />
        ))}
      </QueryState>
    </div>
  );
}
