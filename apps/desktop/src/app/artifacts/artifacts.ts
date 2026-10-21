// What the artifact gallery finds in a session's messages (P24-28): images (markdown images,
// image paths and image URLs), files (the path of every `write_file` / `patch` call) and links
// (other http(s) URLs in the agent's text). Each item remembers the session and profile it came
// from, so the gallery can jump back and main can download it from the right place.

export type ArtifactKind = "image" | "file" | "link";

export interface Artifact {
  readonly key: string;
  readonly kind: ArtifactKind;
  /** A path on the machine that ran the session, or a URL. */
  readonly value: string;
  readonly sessionId: string;
  readonly sessionTitle: string;
  readonly profile: string | null;
  readonly timestamp: number;
}

export interface MessageLike {
  readonly role: string;
  readonly content: string;
  readonly timestamp?: number;
  readonly tool_calls?: ReadonlyArray<{ id?: string; name: string; arguments: string }>;
}

export const FILE_TOOLS: ReadonlySet<string> = new Set([
  "write_file",
  "patch",
  "edit_file",
  "create_file",
]);
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;
const URL_RE = /\bhttps?:\/\/[^\s<>()"'`\]]+/g;
const MD_IMAGE_RE = /!\[[^\]]*\]\(([^)\s]+)\)/g;
const PATH_RE =
  /(?:^|[\s"'(`])((?:\/|~\/|\.\/)[^\s"'`<>()]+\.(?:png|jpe?g|gif|webp|svg|bmp))(?=$|[\s"'`),.;:])/gi;

function trimUrl(url: string): string {
  return url.replace(/[.,;:!?]+$/, "");
}

export function isRemoteValue(value: string): boolean {
  return /^https?:\/\//i.test(value);
}

/** Every artifact in `messages` of one session (deduplicated, in message order). */
export function extractArtifacts(
  messages: readonly MessageLike[],
  session: { id: string; title: string; profile: string | null },
): Artifact[] {
  const found = new Map<string, Artifact>();
  const add = (kind: ArtifactKind, value: string, timestamp: number) => {
    const key = `${session.profile ?? ""}|${session.id}|${kind}|${value}`;
    if (!found.has(key)) {
      found.set(key, {
        key,
        kind,
        value,
        sessionId: session.id,
        sessionTitle: session.title,
        profile: session.profile,
        timestamp,
      });
    }
  };
  for (const message of messages) {
    const when = message.timestamp ?? 0;
    for (const call of message.tool_calls ?? []) {
      if (!FILE_TOOLS.has(call.name)) continue;
      try {
        const args = JSON.parse(call.arguments) as { path?: unknown; file_path?: unknown };
        const path =
          typeof args.path === "string"
            ? args.path
            : typeof args.file_path === "string"
              ? args.file_path
              : null;
        if (path !== null && path !== "") add(IMAGE_EXT.test(path) ? "image" : "file", path, when);
      } catch {
        // arguments that are not JSON name no file
      }
    }
    if (message.role !== "assistant" && message.role !== "tool") continue;
    const text = message.content ?? "";
    const images = new Set<string>();
    for (const match of text.matchAll(MD_IMAGE_RE)) {
      const value = trimUrl(match[1] as string);
      images.add(value);
      add("image", value, when);
    }
    for (const match of text.matchAll(PATH_RE)) add("image", match[1] as string, when);
    if (message.role === "assistant") {
      for (const match of text.matchAll(URL_RE)) {
        const url = trimUrl(match[0]);
        if (images.has(url)) continue;
        let pathname: string;
        try {
          pathname = new URL(url).pathname;
        } catch {
          continue;
        }
        add(IMAGE_EXT.test(pathname) ? "image" : "link", url, when);
      }
    }
  }
  return [...found.values()];
}
