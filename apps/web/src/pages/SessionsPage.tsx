// Sessions (P18-19; look rebuilt W51-a, finished W52-a): figures at the top, one filter row
// (chats / automation / all, a multi-pick source menu, history / overview, search as you type,
// import from another coding tool, compact pager), then either the full history with selection,
// actions, paging and the transcript, or the recent sessions card. The page opens on the history,
// as the reference does (W54-W1-2); search results replace the history list.

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Clock,
  LoaderCircle,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.ts";
import { formatCount } from "../lib/format.ts";
import type { SessionInfo, SessionSearchHit } from "../lib/restTypes.ts";
import { HeaderActions, HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { useToast } from "../shell/Toasts.tsx";
import {
  Button,
  Card,
  CardHeader,
  CountChip,
  IconButton,
  Notice,
  Segmented,
  StatStrip,
} from "../ui/controls.tsx";
import { ConfirmDialog } from "../ui/Dialog.tsx";
import { ImportDialog } from "./sessions/ImportDialog.tsx";
import { PruneDialog } from "./sessions/PruneDialog.tsx";
import { RecentCard, SessionRow, sessionLabel } from "./sessions/SessionRows.tsx";
import { SourceMenu } from "./sessions/SourceMenu.tsx";
import {
  conflictMessage,
  filtersFor,
  inTab,
  SESSION_TABS,
  type SessionTab,
} from "./sessions/sources.ts";

const PAGE_SIZE = 20;
const RECENT_COUNT = 5;
const OVERVIEW_REFRESH_MS = 5_000;
const SEARCH_SETTLE_MS = 300;

type View = "overview" | "history";

const VIEWS: ReadonlyArray<{ value: View; label: string }> = [
  { value: "history", label: "History" },
  { value: "overview", label: "Overview" },
];

function StatsBar({ refresh }: { refresh: boolean }) {
  const stats = useQuery({
    queryKey: ["sessions", "stats"],
    queryFn: () => api.sessions.stats(),
    refetchInterval: refresh ? OVERVIEW_REFRESH_MS : false,
  });
  if (!stats.data) return null;
  const data = stats.data;
  return (
    <StatStrip
      label="Session statistics"
      items={[
        { label: "Total", value: formatCount(data.total) },
        { label: "Active in store", value: formatCount(data.active), tone: "ok" },
        { label: "Archived", value: formatCount(data.archived) },
        { label: "Messages", value: formatCount(data.messages) },
        { label: "Sources", value: formatCount(Object.keys(data.by_source).length) },
      ]}
    />
  );
}

function SearchBox({
  draft,
  busy,
  onDraft,
}: {
  draft: string;
  busy: boolean;
  onDraft: (next: string) => void;
}) {
  return (
    <search className="max-w-[28rem] min-w-60 flex-1 basis-72">
      <div className="flex h-8 items-center gap-2 border border-line pr-1 pl-2.5 focus-within:border-line-strong focus-within:ring-1 focus-within:ring-line-strong">
        {busy ? (
          <LoaderCircle
            aria-hidden="true"
            className="size-3.5 shrink-0 animate-spin text-ink-soft"
          />
        ) : (
          <Search aria-hidden="true" className="size-3.5 shrink-0 text-ink-soft" />
        )}
        <input
          aria-label="Search sessions"
          placeholder="Search message content..."
          className="type-data h-full min-w-0 flex-1 bg-transparent text-[0.75rem] text-ink outline-none focus-visible:outline-none placeholder:tracking-[0.06em] placeholder:text-ink-faint"
          value={draft}
          onChange={(event) => onDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") onDraft("");
          }}
        />
        {draft ? (
          <IconButton
            label="Clear search"
            className="size-6 text-ink-soft hover:text-ink [&_svg]:size-3"
            onClick={() => onDraft("")}
          >
            <X />
          </IconButton>
        ) : null}
      </div>
    </search>
  );
}

function CompactPager({
  page,
  pages,
  onPage,
}: {
  page: number;
  pages: number;
  onPage: (next: number) => void;
}) {
  return (
    <nav aria-label="Pages" className="flex items-center gap-1 text-[0.75rem] text-ink-soft">
      <IconButton label="Previous page" disabled={page === 0} onClick={() => onPage(page - 1)}>
        <ChevronLeft />
      </IconButton>
      <span className="tabular-nums">
        Page {page + 1} of {pages}
      </span>
      <IconButton label="Next page" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>
        <ChevronRight />
      </IconButton>
    </nav>
  );
}

/** One row per session among the search hits (the first snippet wins), in hit order. */
function uniqueHits(hits: readonly SessionSearchHit[]): SessionSearchHit[] {
  const seen = new Set<string>();
  return hits.filter((hit) => {
    if (seen.has(hit.session_id)) return false;
    seen.add(hit.session_id);
    return true;
  });
}

/** A list row for a hit we have no full record of yet. */
function rowFromHit(hit: SessionSearchHit): SessionInfo {
  return {
    id: hit.session_id,
    title: hit.title,
    preview: null,
    source: hit.source,
    model: null,
    started_at: hit.last_active,
    last_active: hit.last_active,
    ended_at: null,
    is_active: false,
    input_tokens: 0,
    output_tokens: 0,
    tool_call_count: 0,
    message_count: 0,
  };
}

export function SessionsPage() {
  const toast = useToast();
  const client = useQueryClient();
  const [tab, setTab] = useState<SessionTab>("chats");
  const [view, setView] = useState<View>("history");
  const [picks, setPicks] = useState<Record<SessionTab, string[]>>({
    chats: [],
    automation: [],
    all: [],
  });
  const [page, setPage] = useState(0);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [highlight, setHighlight] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [anchor, setAnchor] = useState<string | null>(null);
  const [pruneOpen, setPruneOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [deleting, setDeleting] = useState<SessionInfo | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [deleteProblem, setDeleteProblem] = useState("");

  const picked = picks[tab];
  const offset = page * PAGE_SIZE;

  useEffect(() => {
    const next = draft.trim();
    const timer = setTimeout(() => {
      setQuery(next);
      setHighlight(next);
    }, SEARCH_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [draft]);

  const filters = filtersFor(tab, picked);
  const searching = query.length > 0;
  const list = useQuery({
    queryKey: ["sessions", "list", tab, picked.join(","), offset],
    queryFn: () => api.sessions.list({ ...filters, offset, limit: PAGE_SIZE }),
    refetchInterval: view === "overview" && !searching ? OVERVIEW_REFRESH_MS : false,
  });
  const stats = useQuery({ queryKey: ["sessions", "stats"], queryFn: () => api.sessions.stats() });
  const search = useQuery({
    queryKey: ["sessions", "search", query],
    queryFn: () => api.sessions.search(query),
    enabled: searching,
  });

  const rows = list.data?.sessions ?? [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const overviewEmpty = list.isSuccess && rows.length === 0 && page === 0;
  const showToggle = !draft && !overviewEmpty;
  const history = view === "history" || !showToggle;

  // Search hits, filtered like the list (our search endpoint takes no filters).
  const hits = useMemo(
    () =>
      uniqueHits(search.data?.results ?? []).filter((hit) =>
        picked.length > 0 ? picked.includes(hit.source ?? "") : inTab(tab, hit.source),
      ),
    [search.data, picked, tab],
  );
  const loaded = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);
  const missing = hits.filter((hit) => !loaded.has(hit.session_id)).slice(0, PAGE_SIZE);
  const extra = useQueries({
    queries: missing.map((hit) => ({
      queryKey: ["sessions", "one", hit.session_id],
      queryFn: () => api.sessions.one(hit.session_id),
      staleTime: 30_000,
      retry: false,
    })),
  });
  const extraById = new Map<string, SessionInfo>();
  for (const result of extra) if (result.data) extraById.set(result.data.id, result.data);
  const hitRows = hits.map((hit) => ({
    session: loaded.get(hit.session_id) ?? extraById.get(hit.session_id) ?? rowFromHit(hit),
    snippet: hit.snippet,
  }));

  const knownSources = Object.keys(stats.data?.by_source ?? {}).filter((s) => inTab(tab, s));

  const remove = useMutation({
    mutationFn: (id: string) => api.sessions.remove(id),
    onSuccess: (_result, id) => {
      toast.show("Session deleted.", "success");
      setDeleting(null);
      setDeleteProblem("");
      setSelected((was) => was.filter((s) => s !== id));
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => setDeleteProblem(conflictMessage(error)),
  });

  const removeMany = useMutation({
    mutationFn: async (ids: readonly string[]) => {
      const done: string[] = [];
      try {
        for (const id of ids) {
          await api.sessions.remove(id);
          done.push(id);
        }
      } catch (error) {
        setSelected((was) => was.filter((s) => !done.includes(s)));
        void client.invalidateQueries({ queryKey: ["sessions"] });
        throw error;
      }
      return done;
    },
    onSuccess: (done) => {
      toast.show(`Deleted ${done.length} session${done.length === 1 ? "" : "s"}.`, "success");
      setSelected([]);
      setBulkOpen(false);
      setDeleteProblem("");
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => setDeleteProblem(conflictMessage(error)),
  });

  const resetPaging = () => {
    setPage(0);
    setExpanded(null);
    setSelected([]);
    setAnchor(null);
  };

  const openInHistory = (id: string) => {
    setView("history");
    setHighlight("");
    setExpanded(id);
  };

  const select = (id: string, checked: boolean, range: boolean) => {
    const ids = (searching ? hitRows.map((r) => r.session.id) : rows.map((r) => r.id)) as string[];
    if (range && anchor && ids.includes(anchor)) {
      const [a, b] = [ids.indexOf(anchor), ids.indexOf(id)].sort((x, y) => x - y);
      const span = ids.slice(a, (b ?? 0) + 1);
      setSelected((was) =>
        checked ? [...new Set([...was, ...span])] : was.filter((s) => !span.includes(s)),
      );
    } else {
      setSelected((was) => (checked ? [...was, id] : was.filter((s) => s !== id)));
    }
    setAnchor(id);
  };

  const shownRows = searching ? hitRows : rows.map((session) => ({ session, snippet: undefined }));

  const emptyText = searching
    ? "No sessions match your search"
    : tab === "chats" && picked.length === 0
      ? "No sessions yet"
      : "No sessions in this filter";

  return (
    <div className="flex flex-col gap-4 px-4 pt-[1.375rem] pb-[1.4375rem] lg:pr-8 lg:pl-[1.4375rem]">
      <HeaderAfterTitle>
        {list.data ? <CountChip label={`${total} sessions`}>{total}</CountChip> : null}
      </HeaderAfterTitle>
      <HeaderActions>
        <Button icon={<Archive />} onClick={() => setPruneOpen(true)}>
          Prune old sessions
        </Button>
      </HeaderActions>

      <StatsBar refresh={!history} />

      <div className="flex flex-wrap items-center gap-3">
        <Segmented
          label="Show"
          value={tab}
          options={SESSION_TABS}
          onChange={(next) => {
            setTab(next);
            resetPaging();
          }}
        />
        <SourceMenu
          tab={tab}
          sources={knownSources}
          counts={stats.data?.by_source ?? {}}
          picked={picked}
          onChange={(next) => {
            setPicks((was) => ({ ...was, [tab]: next }));
            resetPaging();
          }}
        />
        {showToggle ? (
          <Segmented label="View" value={view} options={VIEWS} onChange={setView} />
        ) : null}
        {history ? (
          <SearchBox
            draft={draft}
            busy={searching && search.isFetching}
            onDraft={(next) => {
              setDraft(next);
              if (!next.trim()) {
                setQuery("");
                setHighlight("");
              }
            }}
          />
        ) : null}
        {draft ? null : (
          <Button icon={<Upload />} onClick={() => setImportOpen(true)}>
            Import sessions
          </Button>
        )}
        {history && !searching && total > PAGE_SIZE ? (
          <div className="ml-auto">
            <CompactPager page={page} pages={pages} onPage={setPage} />
          </div>
        ) : null}
      </div>

      {history && selected.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2 border border-ink/30 bg-ink/[0.06] px-3 py-2">
          <span className="text-[0.75rem] text-ink">{selected.length} selected</span>
          <Button
            tone="ghost"
            size="sm"
            onClick={() =>
              setSelected([...new Set([...selected, ...shownRows.map((r) => r.session.id)])])
            }
          >
            Select all on page
          </Button>
          <Button tone="ghost" size="sm" onClick={() => setSelected([])}>
            Clear selection
          </Button>
          <Button
            size="sm"
            icon={<Trash2 />}
            className="ml-auto border-danger text-danger hover:bg-danger-wash"
            onClick={() => {
              setDeleteProblem("");
              setBulkOpen(true);
            }}
          >
            Delete {selected.length} selected
          </Button>
        </div>
      ) : null}

      {history ? null : (
        <Card padded={false}>
          <CardHeader icon={Clock} title="Recent sessions" />
          <div className="p-4">
            {list.isPending ? (
              <div className="flex justify-center py-6" aria-busy="true">
                <LoaderCircle aria-label="Loading" className="size-5 animate-spin text-ink-soft" />
              </div>
            ) : list.error ? (
              <Notice tone="danger">
                {list.error instanceof Error ? list.error.message : "The list did not load."}
              </Notice>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Recent sessions">
                {rows.slice(0, RECENT_COUNT).map((session) => (
                  <RecentCard
                    key={session.id}
                    session={session}
                    onOpen={() => openInHistory(session.id)}
                  />
                ))}
              </ul>
            )}
          </div>
        </Card>
      )}

      {history ? (
        list.isPending || (searching && search.isPending) ? (
          <div className="flex justify-center py-10" aria-busy="true">
            <LoaderCircle aria-label="Loading" className="size-6 animate-spin text-ink-soft" />
          </div>
        ) : list.error && !searching ? (
          <Notice tone="danger">
            {list.error instanceof Error ? list.error.message : "The list did not load."}
          </Notice>
        ) : shownRows.length === 0 ? (
          <div className="mt-11 flex flex-col items-center gap-3 text-center">
            <Clock aria-hidden="true" className="size-8 text-ink opacity-40" />
            <p className="m-0 text-[0.875rem] font-medium text-ink-muted">{emptyText}</p>
            {emptyText === "No sessions yet" ? (
              <p className="m-0 -mt-1.5 text-[0.75rem] text-ink-soft">
                Chats you start appear here.
              </p>
            ) : null}
          </div>
        ) : (
          <>
            <ul
              className="m-0 flex list-none flex-col gap-1.5 p-0"
              aria-label={searching ? "Search results" : "Sessions"}
            >
              {shownRows.map(({ session, snippet }) => (
                <SessionRow
                  key={session.id}
                  session={session}
                  snippet={snippet}
                  expanded={expanded === session.id}
                  highlight={highlight}
                  selected={selected.includes(session.id)}
                  onSelect={(checked, range) => select(session.id, checked, range)}
                  onToggle={() => {
                    if (!searching) setHighlight("");
                    setExpanded(expanded === session.id ? null : session.id);
                  }}
                  onDelete={() => {
                    setDeleteProblem("");
                    setDeleting(session);
                  }}
                />
              ))}
            </ul>
            {!searching && total > PAGE_SIZE ? (
              <div className="flex items-center justify-between text-[0.75rem] text-ink-soft">
                <span className="tabular-nums">
                  {offset + 1}–{offset + rows.length} of {total}
                </span>
                <CompactPager page={page} pages={pages} onPage={setPage} />
              </div>
            ) : null}
          </>
        )
      ) : null}

      <PruneDialog open={pruneOpen} onClose={() => setPruneOpen(false)} />
      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} />
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
      <ConfirmDialog
        open={bulkOpen}
        title={`Delete ${selected.length} sessions?`}
        description="The picked sessions and their messages are removed for good."
        confirmLabel="Delete"
        danger
        busy={removeMany.isPending}
        onConfirm={() => removeMany.mutate(selected)}
        onCancel={() => {
          setBulkOpen(false);
          setDeleteProblem("");
        }}
      >
        {deleteProblem ? <Notice tone="danger">{deleteProblem}</Notice> : null}
      </ConfirmDialog>
      <span className="sr-only" aria-live="polite">
        {searching && search.isSuccess ? `${hits.length} sessions match` : ""}
      </span>
    </div>
  );
}
