// One session's full history, rendered as light Markdown with each tool call folded away. Each
// message sits on a tinted block by role (user ink, assistant green, tool amber). When opened from
// a search hit, the searched words are marked and the first mark scrolled into view.

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, LoaderCircle } from "lucide-react";
import { Fragment, type ReactNode, useEffect, useRef } from "react";
import { api } from "../../lib/api.ts";
import { relativeTime } from "../../lib/format.ts";
import type { SessionMessage } from "../../lib/restTypes.ts";

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
            <pre
              key={key}
              className="m-0 overflow-x-auto border border-line bg-wash p-2 font-mono text-[0.75rem]"
            >
              <MarkedText text={block.text} term={term} />
            </pre>
          );
        }
        if (block.kind === "heading") {
          // Message headings sit below the page (h1) and section (h2) levels.
          const Heading = HEADING_TAGS[Math.min(3, (block.level ?? 1) - 1)] ?? "h6";
          return (
            <Heading key={key} className="m-0 text-[0.875rem] font-semibold">
              <MarkedText text={block.text} term={term} />
            </Heading>
          );
        }
        return (
          <p key={key} className="m-0 whitespace-pre-wrap">
            <MarkedText text={block.text} term={term} />
          </p>
        );
      })}
    </div>
  );
}

const ROLE_LOOK: Record<SessionMessage["role"], { box: string; label: string; name: string }> = {
  user: { box: "bg-selected", label: "text-ink", name: "User" },
  assistant: { box: "bg-success-wash", label: "text-success", name: "Assistant" },
  system: { box: "bg-wash", label: "text-ink-muted", name: "System" },
  tool: { box: "bg-warning-wash", label: "text-warning", name: "Tool" },
};

/** True when `text` contains any word of the search `term` (case-insensitive). */
function matches(text: string, term: string): boolean {
  const words = term.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return false;
  const lower = text.toLowerCase();
  return words.some((word) => lower.includes(word));
}

function MessageHead({ message, hit }: { message: SessionMessage; hit: boolean }) {
  const look = ROLE_LOOK[message.role];
  const name = message.role === "tool" ? `Tool: ${message.tool_name ?? "unknown"}` : look.name;
  return (
    <header className="flex items-baseline gap-2 text-[0.75rem]">
      <span className={`font-semibold ${look.label}`}>{name}</span>
      {message.timestamp ? (
        <span className="text-ink-soft">{relativeTime(message.timestamp)}</span>
      ) : null}
      {hit ? (
        <span className="type-badge border border-warning/40 bg-warning-wash px-1.5 text-warning">
          match
        </span>
      ) : null}
    </header>
  );
}

function MessageView({ message, term }: { message: SessionMessage; term: string }) {
  const look = ROLE_LOOK[message.role];
  const hit = matches(message.content ?? "", term);
  const ring = hit ? "ring-1 ring-warning/60" : "";
  if (message.role === "tool") {
    return (
      <article className={`flex flex-col gap-1.5 px-3 py-3 ${look.box} ${ring}`}>
        <MessageHead message={message} hit={hit} />
        <pre className="m-0 overflow-x-auto font-mono text-[0.8125rem] whitespace-pre-wrap text-ink">
          <MarkedText text={message.content ?? ""} term={term} />
        </pre>
      </article>
    );
  }
  return (
    <article className={`flex flex-col gap-1.5 px-3 py-3 text-[0.875rem] ${look.box} ${ring}`}>
      <MessageHead message={message} hit={hit} />
      {message.content ? <MarkdownText text={message.content} term={term} /> : null}
      {(message.tool_calls ?? []).map((call) => (
        <details
          key={call.id}
          data-testid={`tool-call-${call.id}`}
          className="group mt-2 border border-warning/20 bg-warning/5 text-[0.75rem]"
        >
          <summary className="flex min-h-[1.9375rem] cursor-pointer list-none items-center gap-2 px-3 py-1.5 font-mono text-warning">
            <ChevronRight
              aria-hidden="true"
              className="size-3 transition-transform group-open:rotate-90"
            />
            <span className="flex-1">{call.name}</span>
            <span className="text-warning/50">{call.id}</span>
          </summary>
          <pre className="m-0 overflow-x-auto border-t border-warning/20 px-3 py-2 font-mono whitespace-pre-wrap text-warning/80">
            {call.arguments}
          </pre>
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
      className="flex max-h-[37.5rem] flex-col gap-3 overflow-y-auto border-t border-line bg-canvas/50 p-4"
      data-testid={`detail-${sessionId}`}
    >
      {messages.isPending ? (
        <div className="flex h-8 items-center justify-center" aria-busy="true">
          <LoaderCircle
            aria-label="Loading messages"
            className="size-4 animate-spin text-ink-soft"
          />
        </div>
      ) : messages.error ? (
        <p role="alert" className="m-0 text-center text-[0.875rem] text-danger">
          {messages.error instanceof Error
            ? messages.error.message
            : "The messages could not be loaded."}
        </p>
      ) : list.length === 0 ? (
        <p className="m-0 text-center text-[0.875rem] text-ink-soft">No messages</p>
      ) : (
        list.map((message, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: history rows are append-only and positional
          <MessageView key={i} message={message} term={highlight} />
        ))
      )}
    </div>
  );
}
