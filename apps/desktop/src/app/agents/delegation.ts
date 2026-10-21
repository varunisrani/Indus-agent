// The live delegation model behind the Agents overlay (P24-28): a pure reducer over a session's
// events. Our protocol has no subagent event yet, so the model reads the parent's tool events:
// `tool.started` of `delegate_task` opens a batch (one worker per entry of `tasks[]`, or one for
// a single `goal`), its `tool.progress` lines update a worker (`[2] terminal: ran pytest` names
// worker 2 and a tool; a path-looking word is a file the worker touched), and `tool.completed`
// settles the batch (each worker done, or failed when the call failed). A future `subagent.update`
// event (`{subagent_id, parent_call_id, index, status?, goal?, tool?, file?}`) is read too, so the
// view gets per-worker truth the day the daemon sends it.

export const DELEGATE_TOOL = "delegate_task";

export type WorkerStatus = "queued" | "running" | "done" | "failed";

export interface Worker {
  readonly index: number;
  readonly goal: string;
  readonly status: WorkerStatus;
  readonly startedAt: number;
  readonly endedAt: number | null;
  readonly tools: number;
  readonly files: readonly string[];
  readonly lastNote: string;
}

export interface Batch {
  readonly callId: string;
  readonly startedAt: number;
  readonly workers: readonly Worker[];
}

export interface DelegationState {
  readonly batches: readonly Batch[];
}

export const EMPTY_DELEGATION: DelegationState = { batches: [] };

export interface DelegationEvent {
  readonly type: string;
  readonly [key: string]: unknown;
}

const NOTE_RE = /^\s*\[(\d+)(?:\s*\/\s*\d+)?\]\s*(.*)$/s;
const TOOL_RE = /^([a-z][a-z0-9_]{1,40}):\s/;
const FILE_RE =
  /(?:^|\s)((?:\.{0,2}\/|~\/)?[\w.-]+(?:\/[\w.-]+)*\.[A-Za-z0-9]{1,8})(?=$|[\s,;:)])/g;

function goalsOf(args: unknown): string[] {
  const record = (args ?? {}) as { tasks?: unknown; goal?: unknown; action?: unknown };
  if (record.action !== undefined && record.action !== "spawn") return [];
  if (Array.isArray(record.tasks)) {
    return record.tasks.map((task, i) => {
      const goal = (task as { goal?: unknown } | null)?.goal;
      return typeof goal === "string" && goal.trim() !== "" ? goal : `Task ${i + 1}`;
    });
  }
  return typeof record.goal === "string" && record.goal.trim() !== "" ? [record.goal] : [];
}

function filesIn(text: string): string[] {
  const found: string[] = [];
  for (const match of text.matchAll(FILE_RE)) {
    const value = match[1] as string;
    if (value.includes("/") || /\.[a-z]{1,5}$/i.test(value)) found.push(value);
  }
  return found;
}

function updateWorker(
  state: DelegationState,
  callId: string,
  index: number | null,
  change: (worker: Worker) => Worker,
): DelegationState {
  let touched = false;
  const batches = state.batches.map((batch) => {
    if (batch.callId !== callId) return batch;
    const workers = batch.workers.map((worker) => {
      if (index !== null && worker.index !== index) return worker;
      if (index === null && batch.workers.length > 1) return worker;
      touched = true;
      return change(worker);
    });
    return { ...batch, workers };
  });
  return touched ? { batches } : state;
}

function noted(worker: Worker, note: string, now: number): Worker {
  const tool = TOOL_RE.exec(note)?.[1];
  const files = [...new Set([...worker.files, ...filesIn(note)])].slice(0, 200);
  return {
    ...worker,
    status: worker.status === "queued" ? "running" : worker.status,
    startedAt: worker.startedAt || now,
    tools: worker.tools + (tool === undefined ? 0 : 1),
    files,
    lastNote: note.slice(0, 300),
  };
}

export function reduceDelegation(
  state: DelegationState,
  event: DelegationEvent,
  now: number,
): DelegationState {
  const callId = typeof event.call_id === "string" ? event.call_id : "";
  if (event.type === "tool.started" && event.name === DELEGATE_TOOL && callId !== "") {
    const goals = goalsOf(event.arguments);
    if (goals.length === 0 || state.batches.some((b) => b.callId === callId)) return state;
    const workers: Worker[] = goals.map((goal, i) => ({
      index: i + 1,
      goal,
      status: "running",
      startedAt: now,
      endedAt: null,
      tools: 0,
      files: [],
      lastNote: "",
    }));
    return { batches: [...state.batches, { callId, startedAt: now, workers }] };
  }
  if (event.type === "tool.progress" && callId !== "") {
    const message = typeof event.message === "string" ? event.message : "";
    const parsed = NOTE_RE.exec(message);
    const index = parsed === null ? null : Number(parsed[1]);
    const note = parsed === null ? message : (parsed[2] as string);
    return updateWorker(state, callId, index, (worker) => noted(worker, note, now));
  }
  if (event.type === "tool.completed" && callId !== "") {
    const failed = event.ok === false;
    return {
      batches: state.batches.map((batch) =>
        batch.callId !== callId
          ? batch
          : {
              ...batch,
              workers: batch.workers.map((worker) =>
                worker.status === "done" || worker.status === "failed"
                  ? worker
                  : { ...worker, status: failed ? "failed" : "done", endedAt: now },
              ),
            },
      ),
    };
  }
  if (event.type === "subagent.update") {
    const parent = typeof event.parent_call_id === "string" ? event.parent_call_id : "";
    const index = typeof event.index === "number" ? event.index : null;
    if (parent === "" || index === null) return state;
    return updateWorker(state, parent, index, (worker) => {
      let next = worker;
      if (typeof event.tool === "string")
        next = { ...next, tools: next.tools + 1, lastNote: event.tool };
      if (typeof event.file === "string" && !next.files.includes(event.file)) {
        next = { ...next, files: [...next.files, event.file] };
      }
      const status = event.status;
      if (status === "queued" || status === "running" || status === "done" || status === "failed") {
        next = { ...next, status, endedAt: status === "done" || status === "failed" ? now : null };
      }
      return next;
    });
  }
  return state;
}

export interface DelegationTotals {
  workers: number;
  active: number;
  tools: number;
  files: number;
}

export function totals(state: DelegationState): DelegationTotals {
  const all = state.batches.flatMap((b) => b.workers);
  return {
    workers: all.length,
    active: all.filter((w) => w.status === "running" || w.status === "queued").length,
    tools: all.reduce((sum, w) => sum + w.tools, 0),
    files: new Set(all.flatMap((w) => w.files)).size,
  };
}
