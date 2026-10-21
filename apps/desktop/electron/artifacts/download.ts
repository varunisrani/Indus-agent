// Artifact downloads through main (P24-28): a file an agent made on a remote Indus is fetched by
// main, never by the window, because the download needs a credential: main mints the single-use
// download ticket on the current connection (`POST /api/files/download-ticket {path}`, sent with
// the artifact's originating profile and session), fetches `GET /api/files/download?path&ticket`
// and writes the bytes into the person's Downloads folder under a name that never overwrites an
// existing file. The window gets back only where the file went; the ticket never leaves main.
//
// The path must be one the artifact gallery found in that session's messages (the renderer sends
// it with the session id; main re-checks only shape here — the server checks the path itself and
// refuses files that hold secrets).

import fs from "node:fs";
import path from "node:path";
import { SESSION_TOKEN_HEADER } from "../backend/localAttach.ts";
import type { Connection } from "../boot/orchestrator.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { IpcRegistry } from "../ipc/registry.ts";
import { isSensitivePath } from "../security/sensitiveFiles.ts";

export const DOWNLOAD_TIMEOUT_MS = 120_000;
export const MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024;

export interface ArtifactRequest {
  path: string;
  profile: string | null;
  sessionId: string;
}

export interface DownloadDeps {
  current(): Connection | null;
  downloadsDir(): string;
  fetch?: typeof fetch;
  log: (line: string) => void;
}

function apiUrl(origin: string, route: string, query: Record<string, string>): string {
  const url = new URL(origin);
  url.pathname = `${url.pathname.replace(/\/+$/, "")}${route}`;
  for (const [key, value] of Object.entries(query))
    if (value !== "") url.searchParams.set(key, value);
  return url.toString();
}

async function credential(connection: Connection, url: string): Promise<Record<string, string>> {
  if (connection.rest.headers !== undefined) return connection.rest.headers(url);
  return { [SESSION_TOKEN_HEADER]: connection.rest.token };
}

/** A file name under `dir` that does not exist yet: `name.ext`, `name (2).ext`, … */
export function freeName(dir: string, wanted: string): string {
  const clean =
    path
      .basename(wanted)
      .split("")
      .map((c) => (c.charCodeAt(0) < 0x20 || '<>:"/\\|?*'.includes(c) ? "_" : c))
      .join("")
      .replace(/_+/g, "_")
      .slice(0, 200) || "artifact";
  const ext = path.extname(clean);
  const stem = clean.slice(0, clean.length - ext.length) || "artifact";
  for (let n = 1; ; n += 1) {
    const name = n === 1 ? `${stem}${ext}` : `${stem} (${n})${ext}`;
    const full = path.join(dir, name);
    if (!fs.existsSync(full)) return full;
  }
}

export async function downloadArtifact(
  deps: DownloadDeps,
  request: ArtifactRequest,
): Promise<{ savedTo: string }> {
  const connection = deps.current();
  if (connection === null) throw new IpcError("not-connected", "Indus is not connected yet");
  if (isSensitivePath(request.path)) {
    throw new IpcError("forbidden", "files that hold secrets are not available here");
  }
  const origin = connection.rest.origin;
  const scope = {
    profile: request.profile ?? connection.descriptor.profile ?? "",
    session_id: request.sessionId,
  };
  const doFetch = deps.fetch ?? fetch;
  const mintUrl = apiUrl(origin, "/api/files/download-ticket", scope);
  let minted: Response;
  try {
    minted = await doFetch(mintUrl, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...(await credential(connection, mintUrl)),
      },
      body: JSON.stringify({ path: request.path }),
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    });
  } catch (error) {
    if (error instanceof IpcError) throw error;
    throw new IpcError("unreachable", "the Indus server did not answer");
  }
  const body = (await minted.json().catch(() => null)) as { ticket?: unknown } | null;
  if (minted.status === 401 || minted.status === 403) {
    throw new IpcError("unauthorized", "sign in to this server again");
  }
  if (minted.status !== 200 || typeof body?.ticket !== "string") {
    throw new IpcError(
      "download-failed",
      `the server would not hand out that file (${minted.status})`,
    );
  }
  const fileUrl = apiUrl(origin, "/api/files/download", {
    path: request.path,
    ticket: body.ticket,
    ...scope,
  });
  let file: Response;
  try {
    file = await doFetch(fileUrl, {
      redirect: "manual",
      signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS),
    });
  } catch {
    throw new IpcError("unreachable", "the download was cut off");
  }
  if (file.status !== 200) {
    await file.body?.cancel();
    throw new IpcError("download-failed", `the download answered ${file.status}`);
  }
  const bytes = Buffer.from(await file.arrayBuffer());
  if (bytes.byteLength > MAX_DOWNLOAD_BYTES) {
    throw new IpcError("too-large", "the file is larger than 256 MiB");
  }
  const dir = deps.downloadsDir();
  fs.mkdirSync(dir, { recursive: true });
  const target = freeName(dir, request.path.split(/[\\/]/).pop() ?? "artifact");
  fs.writeFileSync(target, bytes, { flag: "wx" });
  deps.log(`[artifacts] downloaded one file (${bytes.byteLength} bytes) into Downloads`);
  return { savedTo: target };
}

export function installArtifactsIpc(registry: IpcRegistry, deps: DownloadDeps): void {
  registry.handle("indus:artifacts:download", (request) => downloadArtifact(deps, request));
}
