// The sidebar's sessions (P20-25): rows from the profile's REST list, merged (never replaced),
// one row per compressed conversation, pins and archive through PATCH, search by text and id,
// unread dots that survive a restart, and background-process dots.
//
// Identity: a compressed conversation keeps changing its id (each compression starts a child
// session), so pins, unread and PATCH use its durable id — the chain's root (`lineage_root` on a
// REST row, or found from the parent links) — and the row shows the newest tip.

import {
  familyKey,
  perKeyFamily,
  profileKey,
  type StorageLike,
} from "../profiles/storageFamilies.ts";

/** A session as `GET /api/sessions` and `/api/sessions/search` describe it (fields may be absent). */
export interface SessionInfo {
  readonly id: string;
  readonly title?: string | null;
  readonly preview?: string | null;
  readonly source?: string | null;
  readonly started_at?: number | null;
  readonly last_active?: number | null;
  readonly ended_at?: number | null;
  readonly end_reason?: string | null;
  readonly is_active?: boolean | null;
  readonly archived?: boolean | null;
  readonly pinned?: boolean | null;
  readonly kind?: string | null;
  readonly cwd?: string | null;
  readonly git_branch?: string | null;
  readonly parent_session_id?: string | null;
  readonly message_count?: number | null;
  /** The compression chain's root id (P20-25 addition to the REST row). */
  readonly lineage_root?: string | null;
}

export interface SessionRow {
  /** The id to open (the newest tip). */
  readonly id: string;
  /** Pins, unread and PATCH key on this (the chain's root). */
  readonly durableId: string;
  readonly title: string | null;
  readonly preview: string | null;
  readonly source: string | null;
  /** Epoch milliseconds. */
  readonly lastActive: number;
  readonly startedAt: number;
  readonly active: boolean;
  readonly pinned: boolean;
  readonly archived: boolean;
  /** A branch child: its own titled row. */
  readonly branch: boolean;
  readonly cwd: string | null;
  readonly gitBranch: string | null;
  readonly messageCount: number;
  /** Every id of the conversation (tip and its compressed ancestors). */
  readonly ids: readonly string[];
}

export const COMPRESSION_REASONS: ReadonlySet<string> = new Set([
  "compression",
  "orphaned_compression",
]);

function seconds(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value * 1000 : 0;
}

function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? "").trim();
  return trimmed === "" ? null : trimmed;
}

/** `child` continues `parent` after a compression (a branch or reset never does). */
function continues(child: SessionInfo, parent: SessionInfo | undefined): boolean {
  if (parent === undefined) return false;
  if (child.kind === "branch" || child.kind === "reset" || child.kind === "tool") return false;
  return COMPRESSION_REASONS.has(parent.end_reason ?? "");
}

/**
 * Collapse compression chains to one row each (the tip), keyed by the chain's root. Branch
 * children stay their own rows. Rows come back newest activity first.
 */
export function toRows(infos: readonly SessionInfo[]): SessionRow[] {
  const byId = new Map<string, SessionInfo>();
  for (const info of infos)
    if (typeof info.id === "string" && info.id !== "") byId.set(info.id, info);
  // child → parent links that are compression continuations
  const continuation = new Map<string, string>();
  const continuedBy = new Map<string, string>();
  for (const info of byId.values()) {
    const parentId = info.parent_session_id ?? null;
    if (parentId === null) continue;
    if (continues(info, byId.get(parentId))) {
      continuation.set(info.id, parentId);
      const previous = continuedBy.get(parentId);
      // two children claim the same parent: the newer start is the live tip
      if (
        previous === undefined ||
        seconds(byId.get(previous)?.started_at) < seconds(info.started_at)
      ) {
        continuedBy.set(parentId, info.id);
      }
    }
  }
  const rows = new Map<string, SessionRow>();
  for (const info of byId.values()) {
    // only tips make rows: a session continued by another is folded into it
    if (continuedBy.has(info.id)) continue;
    const chain = [info.id];
    let root = info.id;
    for (
      let parent = continuation.get(root);
      parent !== undefined;
      parent = continuation.get(root)
    ) {
      if (chain.includes(parent)) break; // a loop in bad data
      chain.push(parent);
      root = parent;
    }
    const rootInfo = byId.get(root) as SessionInfo;
    const durableId = text(info.lineage_root) ?? text(rootInfo.lineage_root) ?? root;
    const members = chain.map((id) => byId.get(id) as SessionInfo);
    const lastActive = Math.max(...members.map((m) => seconds(m.last_active ?? m.started_at)));
    const row: SessionRow = {
      id: info.id,
      durableId,
      title: text(info.title) ?? text(rootInfo.title),
      preview: text(info.preview) ?? text(rootInfo.preview),
      source: text(info.source),
      lastActive,
      startedAt: seconds(rootInfo.started_at),
      active: members.some((m) => m.is_active === true),
      // the root row carries the flags the list reads (a REST row already shows them)
      pinned: (rootInfo.pinned ?? info.pinned) === true,
      archived: (rootInfo.archived ?? info.archived) === true,
      branch: info.kind === "branch",
      cwd: text(info.cwd) ?? text(rootInfo.cwd),
      gitBranch: text(info.git_branch) ?? text(rootInfo.git_branch),
      messageCount: members.reduce((sum, m) => sum + (m.message_count ?? 0), 0),
      ids: durableId === root ? chain : [...chain, durableId],
    };
    const existing = rows.get(durableId);
    if (existing === undefined || existing.lastActive <= row.lastActive) rows.set(durableId, row);
  }
  return sortRows([...rows.values()]);
}

export function sortRows(rows: readonly SessionRow[]): SessionRow[] {
  return [...rows].sort((a, b) => b.lastActive - a.lastActive || a.id.localeCompare(b.id));
}

/**
 * Merge a freshly loaded page into what is shown. Rows are matched by durable id (a tip that
 * moved on replaces its older self); rows the page should have contained but did not (deleted,
 * archived elsewhere) are dropped, rows older than the page's window are kept for "load more".
 */
export function mergePage(
  shown: readonly SessionRow[],
  page: readonly SessionRow[],
  options: { complete: boolean },
): SessionRow[] {
  const next = new Map<string, SessionRow>();
  const oldestInPage =
    page.length === 0 ? Number.POSITIVE_INFINITY : Math.min(...page.map((r) => r.lastActive));
  for (const row of shown) {
    if (options.complete) continue; // the page is the whole list
    if (row.lastActive < oldestInPage) next.set(row.durableId, row);
  }
  for (const row of page) next.set(row.durableId, row);
  return sortRows([...next.values()]);
}

/** Replace one row (an optimistic pin/archive, a live update) keyed by durable id. */
export function upsertRow(rows: readonly SessionRow[], row: SessionRow): SessionRow[] {
  return sortRows([...rows.filter((r) => r.durableId !== row.durableId), row]);
}

// -- search --------------------------------------------------------------------------------------

function fold(value: string): string {
  return value.normalize("NFKC").toLowerCase();
}

/** Local match: id (any of the conversation's ids), title, first ask, folder, branch. */
export function rowMatches(row: SessionRow, query: string): boolean {
  const needle = fold(query.trim());
  if (needle === "") return true;
  const haystack = [
    ...row.ids,
    row.durableId,
    row.title ?? "",
    row.preview ?? "",
    row.cwd ?? "",
    row.gitBranch ?? "",
  ];
  return haystack.some((value) => fold(value).includes(needle));
}

/** One server search hit (`/api/sessions/search`). */
export interface SearchHit {
  readonly session_id: string;
  readonly title?: string | null;
  readonly source?: string | null;
  readonly snippet?: string | null;
  readonly last_active?: number | null;
}

/** The server marks matches with `<mark>`; the sidebar shows plain text. */
export function plainSnippet(snippet: string | null | undefined): string | null {
  return text((snippet ?? "").replace(/<\/?mark>/g, ""));
}

/**
 * Search results: an exact id match first, then the server's ranked hits (loaded rows when we
 * have them, a minimal row otherwise), then local matches the server did not return.
 */
export function searchResults(
  rows: readonly SessionRow[],
  query: string,
  hits: readonly SearchHit[] | null,
): SessionRow[] {
  const needle = query.trim();
  if (needle === "") return [];
  const out = new Map<string, SessionRow>();
  const byAnyId = new Map<string, SessionRow>();
  for (const row of rows) for (const id of row.ids) byAnyId.set(id, row);
  const exact = byAnyId.get(needle);
  if (exact !== undefined) out.set(exact.durableId, exact);
  for (const hit of hits ?? []) {
    const known = byAnyId.get(hit.session_id);
    if (known !== undefined) {
      if (!out.has(known.durableId)) out.set(known.durableId, known);
      continue;
    }
    if ([...out.values()].some((row) => row.ids.includes(hit.session_id))) continue;
    const when = seconds(hit.last_active);
    out.set(hit.session_id, {
      id: hit.session_id,
      durableId: hit.session_id,
      title: text(hit.title),
      preview: plainSnippet(hit.snippet),
      source: text(hit.source),
      lastActive: when,
      startedAt: when,
      active: false,
      pinned: false,
      archived: false,
      branch: false,
      cwd: null,
      gitBranch: null,
      messageCount: 0,
      ids: [hit.session_id],
    });
  }
  for (const row of rows) {
    if (!out.has(row.durableId) && rowMatches(row, needle)) out.set(row.durableId, row);
  }
  return [...out.values()];
}

// -- unread --------------------------------------------------------------------------------------

export const UNREAD_LIMIT = 2_000;
const SEEN = perKeyFamily("sidebar-seen");

/**
 * Per-profile watermarks: for each durable id, the message count the person last saw. A row
 * whose count grew past it is unread. A conversation seen for the first time is seeded (no dot),
 * opening one marks it seen; the record is kept in local storage so dots survive a restart.
 */
export class UnreadTracker {
  private readonly storage: StorageLike | null;
  private readonly key: string;
  private seen: Map<string, number>;

  constructor(profile: string | null, storage: StorageLike | null) {
    this.storage = storage;
    this.key = familyKey(SEEN, profileKey(profile));
    this.seen = this.load();
  }

  isUnread(row: SessionRow, openId: string | null = null): boolean {
    if (openId !== null && row.ids.includes(openId)) return false;
    const mark = this.seen.get(row.durableId);
    return mark !== undefined && row.messageCount > mark;
  }

  /** Seed rows never seen before (no dot on a first sight). */
  observe(rows: readonly SessionRow[]): void {
    let changed = false;
    for (const row of rows) {
      if (this.seen.has(row.durableId)) continue;
      this.seen.set(row.durableId, row.messageCount);
      changed = true;
    }
    if (changed) this.save(rows);
  }

  markSeen(row: SessionRow): void {
    if (this.seen.get(row.durableId) === row.messageCount) return;
    this.seen.delete(row.durableId); // re-insert: most recent last
    this.seen.set(row.durableId, row.messageCount);
    this.save([row]);
  }

  /** Mark unread again (the person asked for it). */
  markUnread(row: SessionRow): void {
    this.seen.set(row.durableId, Math.max(0, row.messageCount - 1));
    this.save([row]);
  }

  forget(durableId: string): void {
    if (this.seen.delete(durableId)) this.save([]);
  }

  private load(): Map<string, number> {
    try {
      const raw = this.storage?.getItem(this.key);
      if (raw === null || raw === undefined) return new Map();
      const parsed = JSON.parse(raw) as unknown;
      if (typeof parsed !== "object" || parsed === null) return new Map();
      const out = new Map<string, number>();
      for (const [id, count] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof count === "number" && Number.isFinite(count)) out.set(id, count);
      }
      return out;
    } catch {
      return new Map();
    }
  }

  private save(keep: readonly SessionRow[]): void {
    if (this.seen.size > UNREAD_LIMIT) {
      const keepIds = new Set(keep.map((row) => row.durableId));
      for (const id of this.seen.keys()) {
        if (this.seen.size <= UNREAD_LIMIT) break;
        if (!keepIds.has(id)) this.seen.delete(id);
      }
      // still too many (a huge first page): the oldest entries go
      for (const id of this.seen.keys()) {
        if (this.seen.size <= UNREAD_LIMIT) break;
        this.seen.delete(id);
      }
    }
    try {
      this.storage?.setItem(this.key, JSON.stringify(Object.fromEntries(this.seen)));
    } catch {
      // storage full: dots still work for this run
    }
  }
}

// -- the profile's list -------------------------------------------------------------------------

export type ApiRequest = (payload: {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  body?: unknown;
  profile?: string;
}) => Promise<{ kind: "response" | "not-found"; status: number; body: unknown }>;

export const PAGE_SIZE = 100;
const TURN_STARTED = "turn.started";
const TURN_DONE = new Set(["turn.done"]);

export type ListView = "live" | "archived";

export interface SessionListState {
  readonly rows: readonly SessionRow[];
  readonly view: ListView;
  readonly loading: boolean;
  readonly error: string | null;
  readonly total: number | null;
  /** Sessions with a turn running right now (live events). */
  readonly running: ReadonlySet<string>;
}

function sessionsOf(body: unknown): SessionInfo[] {
  const list = (body as { sessions?: unknown } | null)?.sessions;
  return Array.isArray(list)
    ? list.filter(
        (item): item is SessionInfo =>
          typeof item === "object" && item !== null && typeof (item as SessionInfo).id === "string",
      )
    : [];
}

/** One profile's sessions list, kept while the window lives (lists merge, never replace). */
export class SessionListStore {
  readonly profile: string;
  readonly unread: UnreadTracker;
  private readonly api: ApiRequest | null;
  private readonly listeners = new Set<() => void>();
  private state: SessionListState;
  private generation = 0;
  private writes = 0;

  constructor(profile: string, api: ApiRequest | null, storage: StorageLike | null) {
    this.profile = profileKey(profile);
    this.api = api;
    this.unread = new UnreadTracker(this.profile, storage);
    this.state = {
      rows: [],
      view: "live",
      loading: false,
      error: null,
      total: null,
      running: new Set(),
    };
  }

  getState(): SessionListState {
    return this.state;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  setView(view: ListView): void {
    if (view === this.state.view) return;
    this.patch({ view, rows: [], total: null, error: null });
    void this.refresh();
  }

  /** Load the first page (newest activity first) and merge it in. */
  async refresh(): Promise<void> {
    if (this.api === null) return;
    this.generation += 1;
    const generation = this.generation;
    const view = this.state.view;
    this.patch({ loading: true });
    try {
      const archived = view === "archived" ? "only" : "exclude";
      const answer = await this.api({
        method: "GET",
        path: `/api/sessions?order=recent&archived=${archived}&limit=${PAGE_SIZE}`,
        profile: this.profile,
      });
      if (generation !== this.generation) return;
      if (answer.kind === "not-found") {
        // a backend without the sessions route has nothing to list (not an error)
        this.patch({ loading: false, error: null });
        return;
      }
      if (answer.status >= 400) {
        this.patch({ loading: false, error: `status ${answer.status}` });
        return;
      }
      const page = toRows(sessionsOf(answer.body));
      const total = (answer.body as { total?: unknown }).total;
      const complete = page.length < PAGE_SIZE;
      this.unread.observe(page);
      this.patch({
        rows: mergePage(this.state.rows, page, { complete }),
        loading: false,
        error: null,
        total: typeof total === "number" ? total : null,
      });
    } catch (error) {
      if (generation !== this.generation) return;
      this.patch({ loading: false, error: error instanceof Error ? error.message : String(error) });
    }
  }

  /** Server-side search (ranked hits; null when the server could not answer). */
  async search(query: string): Promise<SearchHit[] | null> {
    if (this.api === null || query.trim() === "") return null;
    try {
      const answer = await this.api({
        method: "GET",
        path: `/api/sessions/search?q=${encodeURIComponent(query.trim())}&limit=20`,
        profile: this.profile,
      });
      if (answer.kind !== "response" || answer.status >= 400) return null;
      const results = (answer.body as { results?: unknown } | null)?.results;
      return Array.isArray(results)
        ? results.filter(
            (hit): hit is SearchHit =>
              typeof hit === "object" &&
              hit !== null &&
              typeof (hit as SearchHit).session_id === "string",
          )
        : null;
    } catch {
      return null;
    }
  }

  setPinned(row: SessionRow, pinned: boolean): Promise<boolean> {
    return this.patchRow(row, { pinned }, { ...row, pinned });
  }

  /** Rename (W52-h): PATCH `{title}`; an empty title is ignored. */
  setTitle(row: SessionRow, title: string): Promise<boolean> {
    const clean = title.trim().slice(0, 200);
    if (clean === "" || clean === row.title) return Promise.resolve(false);
    return this.patchRow(row, { title: clean }, { ...row, title: clean });
  }

  /** Mark the row unread again (the person asked for it). */
  markUnread(row: SessionRow): void {
    this.unread.markUnread(row);
    this.patch({});
  }

  /**
   * Delete the conversation (W52-h): a fenced DELETE of the durable id. The row leaves the list
   * at once and comes back if the backend refuses (a busy session is a 409).
   */
  async remove(row: SessionRow): Promise<boolean> {
    if (this.api === null) return false;
    const before = this.state.rows;
    this.patch({ rows: before.filter((r) => r.durableId !== row.durableId), error: null });
    try {
      const answer = await this.fenced(row.durableId, async (fence) =>
        (this.api as ApiRequest)({
          method: "DELETE",
          path: `/api/sessions/${encodeURIComponent(row.durableId)}?${new URLSearchParams(
            Object.entries(fence).map(([key, value]) => [key, String(value)]),
          ).toString()}`,
          profile: this.profile,
        }),
      );
      if (answer.status >= 200 && answer.status < 400) {
        this.unread.forget(row.durableId);
        return true;
      }
      this.patch({ rows: upsertRow(this.state.rows, row), error: `status ${answer.status}` });
      return false;
    } catch (error) {
      this.patch({
        rows: upsertRow(this.state.rows, row),
        error: error instanceof Error ? error.message : String(error),
      });
      return false;
    }
  }

  setArchived(row: SessionRow, archived: boolean): Promise<boolean> {
    const result = this.patchRow(row, { archived }, { ...row, archived });
    // an archived row leaves the live view (and a restored one the archived view)
    const leaving = (this.state.view === "live") === archived;
    if (leaving) {
      this.patch({ rows: this.state.rows.filter((r) => r.durableId !== row.durableId) });
    }
    return result;
  }

  markSeen(row: SessionRow): void {
    this.unread.markSeen(row);
    this.patch({});
  }

  /** Live events from the profile's connection: running dots and a refresh after a turn. */
  noteEvent(event: { type?: unknown; session_id?: unknown }): boolean {
    const sessionId = typeof event.session_id === "string" ? event.session_id : null;
    if (sessionId === null || typeof event.type !== "string") return false;
    if (event.type === TURN_STARTED) {
      if (this.state.running.has(sessionId)) return false;
      this.patch({ running: new Set([...this.state.running, sessionId]) });
      // a chat the list has not seen yet (a fresh one) is listed while it runs (W54-D1-2)
      return !this.state.rows.some((row) => row.ids.includes(sessionId));
    }
    if (TURN_DONE.has(event.type)) {
      const running = new Set(this.state.running);
      running.delete(sessionId);
      this.patch({ running });
      return true; // the list should refresh (counts, titles)
    }
    return false;
  }

  /** Drop everything (a soft re-home: the rows belonged to the old connection). */
  wipe(): void {
    this.generation += 1;
    this.patch({ rows: [], total: null, error: null, loading: false, running: new Set() });
  }

  /**
   * One fenced write: the backend refuses a write without the session's mutation identity, so
   * read the snapshot (revision, generation) first; a stale revision is read again once.
   */
  private async fenced(
    durableId: string,
    send: (fence: Record<string, string | number>) => ReturnType<ApiRequest>,
  ): Promise<{ status: number }> {
    const api = this.api as ApiRequest;
    const path = `/api/sessions/${encodeURIComponent(durableId)}`;
    let answer: Awaited<ReturnType<ApiRequest>> = { kind: "response", status: 0, body: null };
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const snap = await api({
        method: "GET",
        path: `${path}/mutation-snapshot`,
        profile: this.profile,
      });
      if (snap.kind !== "response" || snap.status >= 400) return { status: snap.status };
      const fence = snap.body as { revision?: unknown; generation?: unknown } | null;
      this.writes += 1;
      answer = await send({
        request_id: `sidebar-${Date.now().toString(36)}-${this.writes}`,
        expected_revision: typeof fence?.revision === "number" ? fence.revision : 0,
        ...(typeof fence?.generation === "number" ? { expected_generation: fence.generation } : {}),
      });
      const code = (answer.body as { detail?: { code?: unknown } } | null)?.detail?.code;
      if (answer.status !== 409 || code !== "stale_revision") break;
    }
    return { status: answer.kind === "not-found" ? 404 : answer.status };
  }

  private fencedPatch(
    durableId: string,
    body: { pinned?: boolean; archived?: boolean; title?: string },
  ): Promise<{ status: number }> {
    return this.fenced(durableId, (fence) =>
      (this.api as ApiRequest)({
        method: "PATCH",
        path: `/api/sessions/${encodeURIComponent(durableId)}`,
        body: { ...body, ...fence },
        profile: this.profile,
      }),
    );
  }

  private async patchRow(
    row: SessionRow,
    body: { pinned?: boolean; archived?: boolean; title?: string },
    optimistic: SessionRow,
  ): Promise<boolean> {
    const before = this.state.rows;
    this.patch({ rows: upsertRow(before, optimistic), error: null });
    if (this.api === null) return false;
    try {
      const answer = await this.fencedPatch(row.durableId, body);
      if (answer.status >= 200 && answer.status < 400) return true;
      this.patch({ rows: upsertRow(this.state.rows, row), error: `status ${answer.status}` });
      return false;
    } catch (error) {
      this.patch({
        rows: upsertRow(this.state.rows, row),
        error: error instanceof Error ? error.message : String(error),
      });
      return false;
    }
  }

  private patch(changes: Partial<SessionListState>): void {
    this.state = { ...this.state, ...changes };
    for (const listener of [...this.listeners]) listener();
  }
}
