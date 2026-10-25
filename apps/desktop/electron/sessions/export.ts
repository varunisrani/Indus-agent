// Session export through main (W56-G1): the sidebar row's "Export". The window sends only the
// session id. Main reads the conversation from the server itself (`GET
// /api/sessions/{id}/export` over the REST proxy, so the current connection's credential and
// profile apply), asks the person where to put it with the system save dialog (parented to the
// window that asked), and writes the file there. No path ever comes from the window, and the
// window gets back only whether a file was written and its name.
//
// The file is JSON Lines — one exported object per line, the shape `indus sessions export`
// writes — so `indus sessions import` reads it back. Mode 0600: a transcript can hold secrets.

import fs from "node:fs";
import path from "node:path";
import type { ApiRequest, ApiResult } from "../api/proxy.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { IpcRegistry } from "../ipc/registry.ts";

export interface SessionExportDeps {
  /** The REST proxy (main's own credential; refuses when nothing is connected). */
  request(request: ApiRequest): Promise<ApiResult>;
  /** The save dialog for the window `senderId`; the chosen file, or null when cancelled. */
  chooseFile(senderId: number, defaultPath: string): Promise<string | null>;
  downloadsDir(): string;
  write?(file: string, text: string): void;
  log(line: string): void;
}

export type ExportAnswer = { saved: false } | { saved: true; fileName: string };

/** A file name that is safe on every platform for `sessionId`. */
export function exportFileName(sessionId: string): string {
  const safe = sessionId.replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 120) || "session";
  return `indus-session-${safe}.jsonl`;
}

function serverMessage(body: unknown): string | null {
  if (body === null || typeof body !== "object") return null;
  const record = body as Record<string, unknown>;
  const detail = record.detail;
  if (detail !== null && typeof detail === "object") {
    const message = (detail as Record<string, unknown>).message;
    if (typeof message === "string" && message !== "") return message;
  }
  if (typeof record.message === "string" && record.message !== "") return record.message;
  if (typeof detail === "string" && detail !== "") return detail;
  return null;
}

/** The export's objects as JSON Lines (one object per line, a final newline). */
export function toJsonLines(body: unknown): string {
  const objects = (body as { objects?: unknown } | null)?.objects;
  if (!Array.isArray(objects) || objects.length === 0) {
    throw new IpcError("not-found", "that conversation has nothing to export");
  }
  return `${objects.map((item) => JSON.stringify(item)).join("\n")}\n`;
}

function defaultWrite(file: string, text: string): void {
  fs.writeFileSync(file, text, { encoding: "utf8", mode: 0o600 });
  // an existing file keeps its old mode with writeFileSync: tighten it as well
  try {
    fs.chmodSync(file, 0o600);
  } catch {
    // a file system without modes (FAT, some network shares): the write itself counted
  }
}

export async function exportSession(
  deps: SessionExportDeps,
  senderId: number,
  sessionId: string,
): Promise<ExportAnswer> {
  const result = await deps.request({
    method: "GET",
    path: `/api/sessions/${encodeURIComponent(sessionId)}/export`,
    timeoutMs: 120_000,
  });
  if (result.kind === "not-found") {
    throw new IpcError("not-found", "no such conversation on this server");
  }
  if (result.status !== 200) {
    throw new IpcError(
      result.status === 413 ? "too-large" : "export-failed",
      serverMessage(result.body) ?? `the server answered ${result.status}`,
    );
  }
  const text = toJsonLines(result.body);
  const file = await deps.chooseFile(
    senderId,
    path.join(deps.downloadsDir(), exportFileName(sessionId)),
  );
  if (file === null || file === "") return { saved: false };
  try {
    (deps.write ?? defaultWrite)(file, text);
  } catch {
    deps.log("[sessions] could not write a session export");
    throw new IpcError("save-failed", "the file could not be written there");
  }
  deps.log(`[sessions] exported one conversation (${Buffer.byteLength(text)} bytes)`);
  return { saved: true, fileName: path.basename(file) };
}

export function installSessionExportIpc(registry: IpcRegistry, deps: SessionExportDeps): void {
  registry.handle("indus:sessions:export", (request, context) =>
    exportSession(deps, context.senderId, request.sessionId),
  );
}
