// The renderer's side of the `indus-media://` scheme (P20-34): the URL a page puts in an image,
// audio or video `src` for a file on the connection's machine. Main decides where the path lives
// (this computer, or the remote host) and whether it may be served.

export const MEDIA_ORIGIN = "indus-media://stream";

export function mediaUrl(absolutePath: string): string {
  return `${MEDIA_ORIGIN}/${encodeURIComponent(absolutePath)}`;
}
