import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  createMediaHandler,
  MediaRoots,
  type MediaTarget,
  mediaTypeFor,
  mediaUrl,
  parseByteRange,
  parseMediaUrl,
} from "./mediaProtocol.ts";

let dir: string;
let outside: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "media-root-"));
  outside = fs.mkdtempSync(path.join(os.tmpdir(), "media-outside-"));
  // 0..99 as bytes, so a range's content is easy to check
  fs.writeFileSync(
    path.join(dir, "clip.mp4"),
    Buffer.from(Array.from({ length: 100 }, (_, i) => i)),
  );
  fs.writeFileSync(path.join(outside, "other.mp4"), Buffer.from("outside"));
  fs.writeFileSync(path.join(dir, "notes.txt"), "text");
  fs.writeFileSync(path.join(dir, "draw.svg"), "<svg/>");
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.rmSync(outside, { recursive: true, force: true });
});

function local(roots: string[] = [dir]) {
  return createMediaHandler({
    roots: new MediaRoots(roots, "linux"),
    target: (): MediaTarget => ({ mode: "local" }),
  });
}

function get(file: string, headers: Record<string, string> = {}, method = "GET") {
  return new Request(mediaUrl(file), { method, headers });
}

describe("media URLs and ranges", () => {
  it("round-trips an absolute path and refuses anything else", () => {
    expect(parseMediaUrl(mediaUrl("/home/me/a b.mp4"))).toBe("/home/me/a b.mp4");
    expect(parseMediaUrl(mediaUrl("C:\\Users\\me\\a.mp4"))).toBe("C:\\Users\\me\\a.mp4");
    expect(parseMediaUrl("indus-media://stream/relative.mp4")).toBeNull();
    expect(parseMediaUrl("indus-media://remote/%2Fx.mp4")).toBeNull();
    expect(parseMediaUrl("file:///etc/passwd")).toBeNull();
    expect(parseMediaUrl("indus-media://stream/%zz")).toBeNull();
  });

  it("parses the three range forms and refuses the rest", () => {
    expect(parseByteRange("bytes=10-19", 100)).toEqual({ start: 10, end: 19 });
    expect(parseByteRange("bytes=90-", 100)).toEqual({ start: 90, end: 99 });
    expect(parseByteRange("bytes=-5", 100)).toEqual({ start: 95, end: 99 });
    expect(parseByteRange("bytes=50-500", 100)).toEqual({ start: 50, end: 99 });
    expect(parseByteRange("bytes=100-", 100)).toBe("unsatisfiable");
    expect(parseByteRange("bytes=20-10", 100)).toBe("unsatisfiable");
    expect(parseByteRange("bytes=-0", 100)).toBe("unsatisfiable");
    expect(parseByteRange("bytes=0-1,5-6", 100)).toBeNull();
    expect(parseByteRange("items=0-1", 100)).toBeNull();
    expect(parseByteRange(null, 100)).toBeNull();
  });

  it("serves images, audio and video only (no SVG)", () => {
    expect(mediaTypeFor("/a/b.PNG")).toBe("image/png");
    expect(mediaTypeFor("/a/b.webm")).toBe("video/webm");
    expect(mediaTypeFor("/a/b.svg")).toBeNull();
    expect(mediaTypeFor("/a/b.html")).toBeNull();
  });
});

describe("local media", () => {
  it("Range bytes=10-19 returns 206 with those 10 bytes", async () => {
    const response = await local()(get(path.join(dir, "clip.mp4"), { Range: "bytes=10-19" }));
    expect(response.status).toBe(206);
    expect(response.headers.get("content-range")).toBe("bytes 10-19/100");
    expect(response.headers.get("content-length")).toBe("10");
    expect(response.headers.get("accept-ranges")).toBe("bytes");
    expect(response.headers.get("content-type")).toBe("video/mp4");
    const bytes = new Uint8Array(await response.arrayBuffer());
    expect([...bytes]).toEqual([10, 11, 12, 13, 14, 15, 16, 17, 18, 19]);
  });

  it("no range → 200 with the whole file; HEAD → headers only; past the end → 416", async () => {
    const whole = await local()(get(path.join(dir, "clip.mp4")));
    expect(whole.status).toBe(200);
    expect((await whole.arrayBuffer()).byteLength).toBe(100);
    const head = await local()(get(path.join(dir, "clip.mp4"), { Range: "bytes=0-9" }, "HEAD"));
    expect(head.status).toBe(206);
    expect(head.headers.get("content-length")).toBe("10");
    expect(await head.text()).toBe("");
    const past = await local()(get(path.join(dir, "clip.mp4"), { Range: "bytes=200-" }));
    expect(past.status).toBe(416);
    expect(past.headers.get("content-range")).toBe("bytes */100");
  });

  it("a path outside the allowed folders → 403, whether or not it exists", async () => {
    expect((await local()(get(path.join(outside, "other.mp4")))).status).toBe(403);
    expect((await local()(get(path.join(outside, "missing.mp4")))).status).toBe(403);
    expect((await local()(get(path.join(dir, "missing.mp4")))).status).toBe(404);
    const walk = await local()(
      new Request(
        `indus-media://stream/${encodeURIComponent(`${dir}/../${path.basename(outside)}/other.mp4`)}`,
      ),
    );
    expect(walk.status).toBe(403);
  });

  it("a symlink inside a root that points outside → 403", async () => {
    const link = path.join(dir, "escape.mp4");
    fs.symlinkSync(path.join(outside, "other.mp4"), link);
    expect((await local()(get(link))).status).toBe(403);
  });

  it("secret files, unknown types and other methods are refused", async () => {
    const keyName = ["id", "ed25519"].join("_");
    fs.mkdirSync(path.join(dir, ".ssh"));
    fs.writeFileSync(path.join(dir, ".ssh", "photo.png"), "x");
    expect((await local()(get(path.join(dir, keyName)))).status).toBe(403);
    expect((await local()(get(path.join(dir, ".ssh", "photo.png")))).status).toBe(403);
    expect((await local()(get(path.join(dir, "notes.txt")))).status).toBe(415);
    expect((await local()(get(path.join(dir, "draw.svg")))).status).toBe(415);
    const post = await local()(get(path.join(dir, "clip.mp4"), {}, "POST"));
    expect(post.status).toBe(405);
    expect(post.headers.get("allow")).toBe("GET, HEAD");
  });

  it("nothing connected → 503", async () => {
    const handler = createMediaHandler({ roots: new MediaRoots([dir]), target: () => null });
    expect((await handler(get(path.join(dir, "clip.mp4")))).status).toBe(503);
  });
});

describe("remote media", () => {
  const token = ["remote", "session", String(Date.now())].join("-");

  function remote(fetchImpl: typeof fetch) {
    return createMediaHandler({
      roots: new MediaRoots([]),
      target: () => ({ mode: "remote", origin: "https://box.example/indus", token, profile: null }),
      fetch: fetchImpl,
    });
  }

  it("mints a ticket with main's credential and streams the file with the range forwarded", async () => {
    const seen: Array<{ url: string; headers: Headers; body?: string }> = [];
    const fake = (async (input: URL | RequestInfo, init?: RequestInit) => {
      const url = String(input);
      const headers = new Headers(init?.headers);
      seen.push({ url, headers, ...(typeof init?.body === "string" ? { body: init.body } : {}) });
      if (url.endsWith("/api/files/download-ticket")) {
        return new Response(JSON.stringify({ ticket: "t-1", expires_in: 60 }), { status: 200 });
      }
      return new Response("0123456789", {
        status: 206,
        headers: {
          "content-range": "bytes 10-19/100",
          "content-type": "application/octet-stream",
          "content-disposition": "attachment; filename=clip.mp4",
          "set-cookie": "a=b",
        },
      });
    }) as typeof fetch;
    const response = await remote(fake)(
      get("/srv/work/clip.mp4", { Range: "bytes=10-19", Cookie: "x=y" }),
    );
    expect(response.status).toBe(206);
    expect(await response.text()).toBe("0123456789");
    expect(response.headers.get("content-type")).toBe("video/mp4");
    expect(response.headers.get("content-disposition")).toBeNull();
    expect(response.headers.get("set-cookie")).toBeNull();
    const [mint, download] = seen;
    expect(mint?.url).toBe("https://box.example/indus/api/files/download-ticket");
    expect(mint?.headers.get("x-indus-session-token")).toBe(token);
    expect(JSON.parse(mint?.body ?? "{}")).toEqual({ path: "/srv/work/clip.mp4" });
    expect(download?.url).toContain("https://box.example/indus/api/files/download?");
    expect(download?.url).toContain("ticket=t-1");
    expect(download?.headers.get("range")).toBe("bytes=10-19");
    expect(download?.headers.get("cookie")).toBeNull();
    expect(download?.headers.get("x-indus-session-token")).toBeNull();
    // the page sees neither credential
    expect([...response.headers.values()].join(" ")).not.toContain(token);
  });

  it("keeps a 401/403 from the remote, maps other failures to 502", async () => {
    const refused = (async () => new Response("{}", { status: 401 })) as typeof fetch;
    expect((await remote(refused)(get("/srv/a.mp4"))).status).toBe(401);
    const broken = (async () => new Response("{}", { status: 500 })) as typeof fetch;
    expect((await remote(broken)(get("/srv/a.mp4"))).status).toBe(502);
    const down = (async () => {
      throw new TypeError("offline");
    }) as typeof fetch;
    expect((await remote(down)(get("/srv/a.mp4"))).status).toBe(502);
  });
});
