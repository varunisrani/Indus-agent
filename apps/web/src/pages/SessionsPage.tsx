// Sessions (P18-19): browse, search, read, rename, export, prune, delete and resume.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../lib/api.ts";
import { formatCount, relativeTime, saveJson } from "../lib/format.ts";
import type { PruneResponse, SessionInfo } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, inputClass, Notice, QueryState, Segmented } from "../ui/controls.tsx";
import { ConfirmDialog, Modal } from "../ui/Dialog.tsx";
import { SessionDetail, SnippetText } from "./sessions/SessionDetail.tsx";
import {
  AUTOMATION_SOURCES,
  conflictMessage,
  filtersFor,
  isAutomation,
  SESSION_TABS,
  type SessionTab,
} from "./sessions/sources.ts";

const PAGE_SIZE = 50;

function sessionLabel(session: {
  title: string | null;
  preview?: string | null;
  id: string;
}): string {
  return session.title || session.preview || session.id;
}

function StatsBar() {
  const stats = useQuery({ queryKey: ["sessions", "stats"], queryFn: () => api.sessions.stats() });
  if (!stats.data) return null;
  const top = Object.entries(stats.data.by_source)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);
  return (
    <dl className="flex flex-wrap gap-4 text-sm" aria-label="Session statistics">
      <div>
        <dt className="text-xs text-muted">Sessions</dt>
        <dd>{formatCount(stats.data.total)}</dd>
      </div>
      <div>
        <dt className="text-xs text-muted">Active now</dt>
        <dd>{formatCount(stats.data.active)}</dd>
      </div>
      <div>
        <dt className="text-xs text-muted">Messages</dt>
        <dd>{formatCount(stats.data.messages)}</dd>
      </div>
      {top.map(([source, count]) => (
        <div key={source}>
          <dt className="text-xs text-muted">{source}</dt>
          <dd>{formatCount(count)}</dd>
        </div>
      ))}
    </dl>
  );
}

function PruneDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const [days, setDays] = useState("90");
  const [preview, setPreview] = useState<PruneResponse | null>(null);
  const [problem, setProblem] = useState("");
  const parsed = Number.parseInt(days, 10);
  const valid = /^\d+$/.test(days.trim()) && Number.isFinite(parsed) && parsed >= 0;

  const dryRun = useMutation({
    mutationFn: () => api.sessions.prune(parsed, true),
    onSuccess: (result) => {
      setPreview(result);
      setProblem("");
    },
    onError: (error) => setProblem(conflictMessage(error)),
  });
  const prune = useMutation({
    mutationFn: () => api.sessions.prune(parsed, false),
    onSuccess: (result) => {
      toast.show(`Removed ${result.count} session${result.count === 1 ? "" : "s"}.`, "success");
      void client.invalidateQueries({ queryKey: ["sessions"] });
      close();
    },
    onError: (error) => setProblem(conflictMessage(error)),
  });

  const close = () => {
    setPreview(null);
    setProblem("");
    onClose();
  };

  return (
    <Modal
      open={open}
      onOpenChange={(next) => (next ? undefined : close())}
      title="Prune old sessions"
      description="Remove ended sessions older than a number of days. Open sessions are never removed."
    >
      <form
        className="flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (valid) dryRun.mutate();
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-muted">
          Older than (days)
          <input
            className={inputClass}
            inputMode="numeric"
            value={days}
            onChange={(event) => {
              setDays(event.currentTarget.value);
              setPreview(null);
            }}
          />
        </label>
        <Button type="submit" disabled={!valid || dryRun.isPending}>
          Check
        </Button>
      </form>
      {!valid ? <p className="mt-2 text-xs text-danger">Enter a whole number of days.</p> : null}
      {preview ? (
        <p className="mt-3 text-sm" data-testid="prune-preview">
          {preview.count} session{preview.count === 1 ? "" : "s"} would be removed
          {preview.skipped_open ? ` (${preview.skipped_open} open sessions kept)` : ""}.
        </p>
      ) : null}
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={close}>Cancel</Button>
        <Button
          tone="danger"
          disabled={!preview || preview.count === 0 || prune.isPending}
          onClick={() => prune.mutate()}
        >
          Prune {preview ? preview.count : ""}
        </Button>
      </div>
    </Modal>
  );
}

function SessionRow({
  session,
  expanded,
  highlight,
  onToggle,
  onDelete,
}: {
  session: SessionInfo;
  expanded: boolean;
  highlight: string;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const toast = useToast();
  const client = useQueryClient();
  const navigate = useNavigate();
  const { selected } = useProfileScope();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title ?? "");

  const rename = useMutation({
    mutationFn: (next: string) => api.sessions.rename(session.id, next),
    onSuccess: () => {
      setEditing(false);
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => toast.show(conflictMessage(error), "error"),
  });

  const exportJson = async () => {
    try {
      saveJson(await api.sessions.exportJson(session.id), `session-${session.id}.json`);
    } catch (error) {
      toast.show(errorText(error), "error");
    }
  };

  const resume = () => {
    const query = new URLSearchParams({ resume: session.id });
    if (selected) query.set("profile", selected);
    navigate(`/chat?${query.toString()}`);
  };

  const submitRename = (event: FormEvent) => {
    event.preventDefault();
    const next = title.trim();
    if (next) rename.mutate(next);
  };

  return (
    <li
      className="flex flex-col gap-2 rounded-md border border-line bg-raised p-3"
      data-testid={`session-${session.id}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {editing ? (
          <form className="flex flex-1 gap-2" onSubmit={submitRename}>
            <input
              aria-label="Session title"
              className={`${inputClass} flex-1`}
              value={title}
              onChange={(event) => setTitle(event.currentTarget.value)}
            />
            <Button type="submit" tone="primary" disabled={rename.isPending}>
              Save
            </Button>
            <Button onClick={() => setEditing(false)}>Cancel</Button>
          </form>
        ) : (
          <button
            type="button"
            className="flex-1 truncate text-left text-sm font-medium"
            onClick={onToggle}
            aria-expanded={expanded}
          >
            {sessionLabel(session)}
          </button>
        )}
        <Badge tone={isAutomation(session.source) ? "warn" : "neutral"}>
          {session.source ?? "unknown"}
        </Badge>
        {session.is_active ? <Badge tone="ok">active</Badge> : null}
        <span className="text-xs text-muted">
          {session.message_count} messages · {relativeTime(session.last_active)}
        </span>
      </div>
      <div className="flex flex-wrap gap-1">
        <Button
          tone="ghost"
          onClick={resume}
          aria-label={`Resume ${sessionLabel(session)} in Chat`}
        >
          ▶ Resume
        </Button>
        <Button tone="ghost" onClick={() => setEditing(true)}>
          Rename
        </Button>
        <Button tone="ghost" onClick={() => void exportJson()}>
          Export JSON
        </Button>
        <Button tone="ghost" onClick={onDelete}>
          Delete
        </Button>
      </div>
      {expanded ? <SessionDetail sessionId={session.id} highlight={highlight} /> : null}
    </li>
  );
}

export function SessionsPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [tab, setTab] = useState<SessionTab>("chats");
  const [source, setSource] = useState("");
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [highlight, setHighlight] = useState("");
  const [pruneOpen, setPruneOpen] = useState(false);
  const [deleting, setDeleting] = useState<SessionInfo | null>(null);
  const [deleteProblem, setDeleteProblem] = useState("");

  const filters = filtersFor(tab, source);
  const list = useQuery({
    queryKey: ["sessions", "list", tab, source, offset],
    queryFn: () => api.sessions.list({ ...filters, offset, limit: PAGE_SIZE }),
  });
  const stats = useQuery({ queryKey: ["sessions", "stats"], queryFn: () => api.sessions.stats() });
  const search = useQuery({
    queryKey: ["sessions", "search", query],
    queryFn: () => api.sessions.search(query),
    enabled: query.length > 0,
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.sessions.remove(id),
    onSuccess: () => {
      toast.show("Session deleted.", "success");
      setDeleting(null);
      setDeleteProblem("");
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => setDeleteProblem(conflictMessage(error)),
  });

  const knownSources = Object.keys(stats.data?.by_source ?? {}).filter((s) => {
    if (tab === "chats") return !isAutomation(s);
    if (tab === "automation")
      return isAutomation(s) || (AUTOMATION_SOURCES as readonly string[]).includes(s);
    return true;
  });

  const openHit = (id: string) => {
    setExpanded(id);
    setHighlight(query);
  };

  const rows = list.data?.sessions ?? [];
  const total = list.data?.total ?? 0;

  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <Button onClick={() => setPruneOpen(true)}>Prune…</Button>
      </HeaderActions>
      <StatsBar />
      <search>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setQuery(draft.trim());
          }}
        >
          <input
            aria-label="Search sessions"
            placeholder="Search every message"
            className={`${inputClass} flex-1`}
            value={draft}
            onChange={(event) => setDraft(event.currentTarget.value)}
          />
          <Button type="submit">Search</Button>
          {query ? (
            <Button
              onClick={() => {
                setQuery("");
                setDraft("");
                setHighlight("");
              }}
            >
              Clear
            </Button>
          ) : null}
        </form>
      </search>

      {query ? (
        <Card>
          <h2 className="mb-2 text-sm font-semibold">Matches for “{query}”</h2>
          <QueryState
            loading={search.isPending}
            error={search.error}
            empty={(search.data?.results.length ?? 0) === 0}
            emptyText="No message matches that search."
          >
            <ul className="flex flex-col gap-2" aria-label="Search results">
              {(search.data?.results ?? []).map((hit) => (
                <li key={`${hit.session_id}-${hit.snippet}`}>
                  <button
                    type="button"
                    className="text-left text-sm"
                    onClick={() => openHit(hit.session_id)}
                  >
                    <span className="font-medium">{hit.title ?? hit.session_id}</span>
                    <span className="block text-muted">
                      <SnippetText snippet={hit.snippet} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </QueryState>
          {expanded && search.data?.results.some((h) => h.session_id === expanded) ? (
            <SessionDetail sessionId={expanded} highlight={highlight} />
          ) : null}
        </Card>
      ) : null}

      <div className="flex flex-wrap items-center gap-4">
        <Segmented
          label="Show"
          value={tab}
          options={SESSION_TABS}
          onChange={(next) => {
            setTab(next);
            setSource("");
            setOffset(0);
          }}
        />
        <label className="flex items-center gap-2 text-xs text-muted">
          Source
          <select
            className={inputClass}
            value={source}
            onChange={(event) => {
              setSource(event.currentTarget.value);
              setOffset(0);
            }}
          >
            <option value="">Any source</option>
            {knownSources.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>

      <QueryState
        loading={list.isPending}
        error={list.error}
        empty={rows.length === 0}
        emptyText="No sessions here."
      >
        <ul className="flex flex-col gap-2" aria-label="Sessions">
          {rows.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              expanded={expanded === session.id && !query}
              highlight={highlight}
              onToggle={() => {
                setHighlight("");
                setExpanded(expanded === session.id ? null : session.id);
              }}
              onDelete={() => {
                setDeleteProblem("");
                setDeleting(session);
              }}
            />
          ))}
        </ul>
        <div className="flex items-center gap-2 text-xs text-muted">
          <Button
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </Button>
          <span>
            {offset + 1}–{offset + rows.length} of {total}
          </span>
          <Button
            disabled={offset + rows.length >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      </QueryState>

      <PruneDialog open={pruneOpen} onClose={() => setPruneOpen(false)} />
      <ConfirmDialog
        open={deleting !== null}
        title="Delete this session?"
        description={
          deleting ? `“${sessionLabel(deleting)}” and its messages are removed for good.` : ""
        }
        confirmLabel="Delete"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (deleting) remove.mutate(deleting.id);
        }}
        onCancel={() => {
          setDeleting(null);
          setDeleteProblem("");
        }}
      >
        {deleteProblem ? <Notice tone="danger">{deleteProblem}</Notice> : null}
      </ConfirmDialog>
    </div>
  );
}
