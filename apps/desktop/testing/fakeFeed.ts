// A loopback releases feed for the update tests (P20-37): answers GET <base>/releases with a
// GitHub-shaped JSON list, records what each request carried, and can refuse a token. No real
// network is ever touched.

import http from "node:http";
import type { AddressInfo } from "node:net";

export interface FakeRelease {
  tag_name: string;
  draft?: boolean;
  prerelease?: boolean;
  html_url?: string;
}

export interface FeedRequest {
  path: string;
  authorization: string | null;
}

export class FakeFeed {
  releases: FakeRelease[] = [];
  /** Authorization values answered with 401. */
  readonly rejected = new Set<string>();
  /** Answer every request with this status (e.g. 503). */
  status: number | null = null;
  readonly requests: FeedRequest[] = [];
  private server: http.Server | null = null;
  url = "";

  async start(): Promise<string> {
    this.server = http.createServer((request, response) => {
      const authorization = request.headers.authorization ?? null;
      this.requests.push({ path: request.url ?? "", authorization });
      if (this.status !== null) {
        response.writeHead(this.status).end("{}");
        return;
      }
      if (authorization !== null && this.rejected.has(authorization)) {
        response
          .writeHead(401, { "content-type": "application/json" })
          .end('{"message":"Bad credentials"}');
        return;
      }
      if (!(request.url ?? "").startsWith("/releases")) {
        response.writeHead(404).end();
        return;
      }
      response
        .writeHead(200, { "content-type": "application/json" })
        .end(JSON.stringify(this.releases));
    });
    await new Promise<void>((resolve) => this.server?.listen(0, "127.0.0.1", resolve));
    const { port } = this.server.address() as AddressInfo;
    this.url = `http://127.0.0.1:${port}/releases`;
    return this.url;
  }

  async stop(): Promise<void> {
    const server = this.server;
    this.server = null;
    if (server === null) return;
    server.closeAllConnections?.();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}
