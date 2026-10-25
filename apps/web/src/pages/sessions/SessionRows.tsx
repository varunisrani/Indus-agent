// The two ways a session is listed: a card in the Overview ("recent sessions": title, model and
// counts, the opening line) and a row in History (checkbox, source icon, title that opens the
// transcript, a live pill, counts, a search snippet, and icon actions for resume, rename, export
// and delete). Rename happens in place.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Download, LoaderCircle, Pencil, Play, Trash2, X } from "lucide-react";
import { type MouseEvent, type ReactNode, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../../lib/api.ts";
import { relativeTime, saveJson } from "../../lib/format.ts";
import type { SessionInfo } from "../../lib/restTypes.ts";
import { useProfileScope } from "../../shell/ProfileScope.tsx";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import { Badge, IconButton, inputClass } from "../../ui/controls.tsx";
import { cx } from "../../ui/cx.ts";
import { SessionDetail, SnippetText } from "./SessionDetail.tsx";
import { SourceIcon } from "./SourceIcon.tsx";
import { conflictMessage, sourceLabel } from "./sources.ts";

export function sessionLabel(session: {
  title: string | null;
  preview?: string | null;
  id: string;
}): string {
  return session.title || session.preview || session.id;
}

/** The title, or the opening line in italics when the session has no title yet. */
function TitleText({ session }: { session: SessionInfo }) {
  if (session.title) return <>{session.title}</>;
  const preview = session.preview?.slice(0, 60);
  return preview ? <em className="text-ink-muted">{preview}</em> : session.id;
}

export function SourceBadge({ source }: { source: string | null }) {
  return (
    <Badge icon={<SourceIcon source={source} tinted={false} />} className="text-ink-muted">
      {sourceLabel(source)}
    </Badge>
  );
}

/** The green "live" pill with a pulsing dot. */
export function LivePill() {
  return (
    <Badge tone="ok" className="h-5 px-1.5">
      <span aria-hidden="true" className="relative flex size-1.5">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-success opacity-60" />
        <span className="relative inline-flex size-1.5 rounded-full bg-success" />
      </span>
      live
    </Badge>
  );
}

/** "model · 4 msgs · 2 tools · 3h ago": the model in mono, gaps between the parts. */
function MetaLine({ session, tools }: { session: SessionInfo; tools: boolean }) {
  const parts: ReactNode[] = [];
  if (session.model) {
    parts.push(
      <span key="model" className="type-data text-[0.75rem]">
        {session.model.split("/").at(-1)}
      </span>,
    );
  }
  parts.push(<span key="msgs">{session.message_count} msgs</span>);
  if (tools && session.tool_call_count > 0) {
    parts.push(<span key="tools">{session.tool_call_count} tools</span>);
  }
  parts.push(<span key="when">{relativeTime(session.last_active)}</span>);
  return (
    <p className="m-0 flex flex-wrap items-baseline gap-x-3.5 text-[0.75rem] text-ink-muted">
      {parts}
    </p>
  );
}

/** Overview card. Clicking the title opens the session in History. */
export function RecentCard({ session, onOpen }: { session: SessionInfo; onOpen: () => void }) {
  return (
    <li
      className="flex items-center gap-3 border border-line px-3 py-3"
      data-testid={`recent-${session.id}`}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <button
          type="button"
          onClick={onOpen}
          className="type-serif self-start truncate text-left text-[0.875rem] font-medium text-ink hover:underline"
        >
          <TitleText session={session} />
        </button>
        <MetaLine session={session} tools={false} />
        {session.title && session.preview && session.preview !== session.title ? (
          <p className="type-serif m-0 line-clamp-2 text-[0.75rem] text-ink-soft">
            {session.preview}
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {session.is_active ? <LivePill /> : null}
        <SourceBadge source={session.source} />
      </div>
    </li>
  );
}

function RenameForm({ session, onDone }: { session: SessionInfo; onDone: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const [title, setTitle] = useState(session.title ?? "");
  const rename = useMutation({
    mutationFn: (next: string) => api.sessions.rename(session.id, next),
    onSuccess: () => {
      onDone();
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => toast.show(conflictMessage(error), "error"),
  });
  const save = () => {
    const next = title.trim();
    if (!next || next === (session.title ?? "")) onDone();
    else rename.mutate(next);
  };
  return (
    <form
      className="flex items-center gap-1"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onDone();
        }
      }}
    >
      <input
        aria-label="Session title"
        // biome-ignore lint/a11y/noAutofocus: the field appears because the person asked to rename
        autoFocus
        className={cx(inputClass, "h-7 min-h-7 min-w-48 flex-1 text-[0.8125rem]")}
        value={title}
        onChange={(event) => setTitle(event.currentTarget.value)}
      />
      <IconButton label="Save" type="submit" disabled={rename.isPending}>
        {rename.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}
      </IconButton>
      <IconButton label="Cancel rename" onClick={onDone}>
        <X />
      </IconButton>
    </form>
  );
}

/** History row with its actions; the transcript opens underneath when expanded. */
export function SessionRow({
  session,
  expanded,
  highlight,
  snippet,
  selected,
  onSelect,
  onToggle,
  onDelete,
}: {
  session: SessionInfo;
  expanded: boolean;
  highlight: string;
  /** A search hit's snippet (with `<mark>` tags) shown under the meta line. */
  snippet?: string | undefined;
  selected: boolean;
  /** `range` is true for a shift-click (select everything since the last pick). */
  onSelect: (checked: boolean, range: boolean) => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const toast = useToast();
  const navigate = useNavigate();
  const { selected: profile } = useProfileScope();
  const [editing, setEditing] = useState(false);
  const label = sessionLabel(session);

  const exportJson = async () => {
    try {
      saveJson(await api.sessions.exportJson(session.id), `session-${session.id}.json`);
    } catch (error) {
      toast.show(errorText(error), "error");
    }
  };

  const resume = () => {
    const query = new URLSearchParams({ resume: session.id });
    if (profile) query.set("profile", profile);
    navigate(`/chat?${query.toString()}`);
  };

  const stop = (run: () => void) => (event: MouseEvent) => {
    event.stopPropagation();
    run();
  };

  return (
    <li
      className={cx(
        "border transition-colors",
        selected
          ? "border-ink/40 bg-ink/[0.03]"
          : session.is_active
            ? "border-success/25 bg-success/[0.03]"
            : "border-line",
      )}
      data-testid={`session-${session.id}`}
    >
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: the title button is the keyboard path */}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: a larger mouse target for that button */}
      <div
        className="flex cursor-pointer items-start gap-3 px-3 py-3 hover:bg-hover/40"
        onClick={() => {
          if (!editing) onToggle();
        }}
      >
        <input
          type="checkbox"
          aria-label={`Select ${label}`}
          checked={selected}
          onClick={(event) => {
            event.stopPropagation();
            onSelect(event.currentTarget.checked, event.shiftKey);
          }}
          onChange={() => {}}
          className="mt-0.5 size-4 shrink-0 cursor-pointer appearance-none rounded-[2px] border border-line-strong bg-transparent checked:border-accent checked:bg-accent"
        />
        <SourceIcon source={session.source} className="mt-0.5 size-4 shrink-0" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          {editing ? (
            <RenameForm session={session} onDone={() => setEditing(false)} />
          ) : (
            <span className="flex min-w-0 items-center gap-2">
              <button
                type="button"
                className="type-serif min-w-0 truncate text-left text-[0.875rem] text-ink hover:underline"
                onClick={stop(onToggle)}
                aria-expanded={expanded}
                aria-label={label}
              >
                <TitleText session={session} />
              </button>
              {session.is_active ? <LivePill /> : null}
            </span>
          )}
          <MetaLine session={session} tools />
          {snippet ? (
            <p className="type-serif m-0 truncate text-[0.75rem] text-ink-soft">
              <SnippetText snippet={snippet} />
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-2 self-center">
          <SourceBadge source={session.source} />
          <div className="flex items-center gap-0.5 [&_button]:size-8 [&_button]:text-ink-muted">
            <IconButton
              label={`Resume ${label} in Chat`}
              className="hover:!text-success"
              onClick={stop(resume)}
            >
              <Play />
            </IconButton>
            <IconButton label="Rename" onClick={stop(() => setEditing(true))}>
              <Pencil />
            </IconButton>
            <IconButton label="Export JSON" onClick={stop(() => void exportJson())}>
              <Download />
            </IconButton>
            <IconButton
              label="Delete"
              tone="danger"
              className="!text-danger"
              onClick={stop(onDelete)}
            >
              <Trash2 />
            </IconButton>
          </div>
        </div>
      </div>
      {expanded ? <SessionDetail sessionId={session.id} highlight={highlight} /> : null}
    </li>
  );
}
