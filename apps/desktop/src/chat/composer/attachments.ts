// Composer attachments (P20-19, R08.23).
//
// A sandboxed renderer never sees the path of a dropped or pasted `File`, so those always travel as
// bytes: uploaded through main's REST proxy (`POST /api/files/upload`) into the session's folder on
// whichever host runs the session, and the agent gets that host's path. A file picked in the native
// dialog has a client path: in local mode main's fs guard checks it and the path goes as is; in
// remote mode its bytes are read (through the same guard) and uploaded — a client path never
// reaches a remote host. Images then go to `image.attach`; other files become an `@path`
// reference in the message.

import type { MethodName, MethodTypes } from "@indus/protocol";
import type { ConnectionMode, RequestOptions } from "../../gateway/connectionStore.ts";

/** Attachments larger than this are refused before anything is read (P20-14's cap). */
export const MAX_ATTACHMENT_BYTES = 256 * 1024 * 1024;
/** Where uploads land, relative to the session's folder. */
export const UPLOAD_DIR = ".indus/attachments";

export type AttachmentSource =
  | { readonly kind: "path"; readonly path: string }
  | { readonly kind: "bytes"; read(): Promise<Uint8Array> };

export interface Attachment {
  readonly id: string;
  readonly name: string;
  readonly size: number;
  readonly mimeType: string;
  readonly source: AttachmentSource;
}

export interface StagedAttachment {
  readonly attachment: Attachment;
  /** The path on the host that runs the session. */
  readonly hostPath: string;
  readonly image: boolean;
}

export class AttachmentError extends Error {
  readonly attachment: Attachment;
  constructor(attachment: Attachment, message: string) {
    super(message);
    this.name = "AttachmentError";
    this.attachment = attachment;
  }
}

type Request = <M extends MethodName>(
  method: M,
  params: MethodTypes[M]["params"],
  options?: RequestOptions,
) => Promise<MethodTypes[M]["result"]>;

export interface StageDeps {
  readonly mode: ConnectionMode;
  readonly sessionId: string;
  /** The session's folder on its host (uploads go below it); null uses the host's default. */
  readonly cwd: string | null;
  readonly request: Request;
  /** Throws when main's fs guard refuses a local path. */
  guardLocalPath(path: string): Promise<void>;
  /** Bytes of a client path, read through main's guard. */
  readLocalBytes(path: string): Promise<Uint8Array>;
  /** Upload bytes; resolves with the stored path on the session's host. */
  uploadBytes(target: string, name: string, mimeType: string, bytes: Uint8Array): Promise<string>;
}

export function isImage(attachment: Attachment): boolean {
  return attachment.mimeType.startsWith("image/");
}

/** How an attachment travels: its client path (local mode, known path) or its bytes. */
export function transportFor(attachment: Attachment, mode: ConnectionMode): "path" | "bytes" {
  return attachment.source.kind === "path" && mode === "local" ? "path" : "bytes";
}

function safeName(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? "file";
  const cleaned = base.replace(/[^\w.\- ]+/g, "_").replace(/^\.+/, "");
  return cleaned === "" ? "file" : cleaned.slice(0, 120);
}

function joinHost(cwd: string | null, relative: string): string {
  if (cwd === null || cwd === "") return relative;
  const sep = cwd.includes("\\") && !cwd.includes("/") ? "\\" : "/";
  return `${cwd.replace(/[\\/]+$/, "")}${sep}${relative.replaceAll("/", sep)}`;
}

/** Make one attachment available to the session's host. */
export async function stageAttachment(
  attachment: Attachment,
  deps: StageDeps,
): Promise<StagedAttachment> {
  if (attachment.size > MAX_ATTACHMENT_BYTES) {
    throw new AttachmentError(attachment, "larger than 256 MiB");
  }
  const image = isImage(attachment);
  let hostPath: string;
  if (transportFor(attachment, deps.mode) === "path" && attachment.source.kind === "path") {
    await deps.guardLocalPath(attachment.source.path);
    hostPath = attachment.source.path;
  } else {
    const bytes =
      attachment.source.kind === "path"
        ? await deps.readLocalBytes(attachment.source.path)
        : await attachment.source.read();
    if (bytes.byteLength > MAX_ATTACHMENT_BYTES) {
      throw new AttachmentError(attachment, "larger than 256 MiB");
    }
    const target = joinHost(
      deps.cwd,
      `${UPLOAD_DIR}/${attachment.id}-${safeName(attachment.name)}`,
    );
    hostPath = await deps.uploadBytes(
      target,
      safeName(attachment.name),
      attachment.mimeType,
      bytes,
    );
  }
  if (image) {
    await deps.request("image.attach", { session_id: deps.sessionId, path: hostPath });
  }
  return { attachment, hostPath, image };
}

/** The message text with `@path` references for the staged non-image files. */
export function withFileReferences(text: string, staged: readonly StagedAttachment[]): string {
  const refs = staged.filter((item) => !item.image).map((item) => `@${item.hostPath}`);
  if (refs.length === 0) return text;
  return text.trim() === "" ? refs.join(" ") : `${text.trimEnd()}\n${refs.join(" ")}`;
}

let counter = 0;
function nextId(): string {
  counter += 1;
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : String(Date.now());
  return `${random}${counter}`;
}

/** Attachments from dropped or pasted files: always bytes (no path in a sandboxed renderer). */
export function attachmentsFromFiles(files: Iterable<File>): Attachment[] {
  return [...files].map((file) => ({
    id: nextId(),
    name: file.name || "pasted",
    size: file.size,
    mimeType: file.type || "application/octet-stream",
    source: { kind: "bytes", read: async () => new Uint8Array(await file.arrayBuffer()) },
  }));
}

/** An attachment for a path chosen in the native picker. */
export function attachmentFromPath(path: string, size = 0, mimeType = guessMime(path)): Attachment {
  return {
    id: nextId(),
    name: path.split(/[\\/]/).pop() ?? path,
    size,
    mimeType,
    source: { kind: "path", path },
  };
}

const IMAGE_TYPES: Readonly<Record<string, string>> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
};

export function guessMime(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_TYPES[ext] ?? "application/octet-stream";
}

// -- the desktop's default helpers, over the preload bridge -------------------------------------------

interface BridgeSubset {
  fs: {
    readText(payload: { path: string; maxBytes?: number }): Promise<unknown>;
    readDataUrl(payload: {
      path: string;
      purpose: "preview" | "attach";
    }): Promise<{ dataUrl: string }>;
  };
  api: {
    request(payload: {
      method: "POST";
      path: string;
      body?: unknown;
      timeoutMs?: number;
    }): Promise<{ kind: string; status: number; body: unknown }>;
  };
}

function bridge(): BridgeSubset {
  const found = (globalThis as { indusDesktop?: BridgeSubset }).indusDesktop;
  if (found === undefined) throw new Error("the desktop bridge is not available");
  return found;
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let at = 0; at < bytes.length; at += chunk) {
    binary += String.fromCharCode(...bytes.subarray(at, at + chunk));
  }
  return btoa(binary);
}

function fromDataUrl(dataUrl: string): Uint8Array {
  const comma = dataUrl.indexOf(",");
  const binary = atob(dataUrl.slice(comma + 1));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Main's fs guard accepts the path when a one-byte read through it succeeds. */
export async function bridgeGuardLocalPath(path: string): Promise<void> {
  await bridge().fs.readText({ path, maxBytes: 1 });
}

export async function bridgeReadLocalBytes(path: string): Promise<Uint8Array> {
  const { dataUrl } = await bridge().fs.readDataUrl({ path, purpose: "attach" });
  return fromDataUrl(dataUrl);
}

/** Upload through main's REST proxy; main encodes the multipart body and adds the credential. */
export async function bridgeUploadBytes(
  target: string,
  name: string,
  mimeType: string,
  bytes: Uint8Array,
): Promise<string> {
  const answer = await bridge().api.request({
    method: "POST",
    path: "/api/files/upload",
    body: {
      multipart: {
        path: target,
        overwrite: false,
        file: { name, type: mimeType, base64: toBase64(bytes) },
      },
    },
    timeoutMs: 120_000,
  });
  if (answer.kind !== "response" || answer.status >= 400) {
    throw new Error(`upload failed (${answer.status})`);
  }
  const stored = (answer.body as { path?: unknown } | null)?.path;
  return typeof stored === "string" && stored !== "" ? stored : target;
}
