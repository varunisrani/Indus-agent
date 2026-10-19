// Liveness for links that can die silently. Any inbound frame proves the peer alive (R05.9).

import { type Clock, systemClock, type TimerHandle } from "./clock.ts";
import {
  HEARTBEAT_DEADLINE_MS,
  HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_MAX_OUTSTANDING,
} from "./limits.ts";

export interface HeartbeatOptions {
  clock?: Clock;
  intervalMs?: number;
  deadlineMs?: number;
  maxOutstanding?: number;
  /** Send one ping; the promise settles when its answer (or error) comes back. */
  sendPing: () => Promise<unknown>;
  /** No inbound frame for the whole deadline, or a ping could not be sent. */
  onDead: (why: string) => void;
}

export class Heartbeat {
  private readonly clock: Clock;
  private readonly intervalMs: number;
  private readonly deadlineMs: number;
  private readonly maxOutstanding: number;
  private readonly sendPing: () => Promise<unknown>;
  private readonly onDead: (why: string) => void;
  private pingTimer: TimerHandle | undefined;
  private deadlineTimer: TimerHandle | undefined;
  private lastInbound = 0;
  private outstanding = 0;
  private running = false;
  private generation = 0;
  /** Pings sent since start, for tests and diagnostics. */
  sent = 0;

  constructor(options: HeartbeatOptions) {
    this.clock = options.clock ?? systemClock;
    this.intervalMs = options.intervalMs ?? HEARTBEAT_INTERVAL_MS;
    this.deadlineMs = options.deadlineMs ?? HEARTBEAT_DEADLINE_MS;
    this.maxOutstanding = options.maxOutstanding ?? HEARTBEAT_MAX_OUTSTANDING;
    this.sendPing = options.sendPing;
    this.onDead = options.onDead;
  }

  get active(): boolean {
    return this.running;
  }

  get unanswered(): number {
    return this.outstanding;
  }

  start(): void {
    this.stop();
    this.generation += 1;
    this.running = true;
    this.lastInbound = this.clock.now();
    this.armPing();
    this.armDeadline(this.deadlineMs);
  }

  stop(): void {
    this.running = false;
    if (this.pingTimer !== undefined) this.clock.clearTimeout(this.pingTimer);
    if (this.deadlineTimer !== undefined) this.clock.clearTimeout(this.deadlineTimer);
    this.pingTimer = undefined;
    this.deadlineTimer = undefined;
    this.outstanding = 0;
  }

  /** Call for every inbound frame: events, answers, anything. Cheap: no timer work here. */
  noteInbound(): void {
    this.lastInbound = this.clock.now();
  }

  private armPing(): void {
    this.pingTimer = this.clock.setTimeout(() => {
      if (!this.running) return;
      this.ping();
      if (this.running) this.armPing();
    }, this.intervalMs);
  }

  private ping(): void {
    if (this.outstanding >= this.maxOutstanding) return;
    this.outstanding += 1;
    this.sent += 1;
    let settled = false;
    const generation = this.generation;
    const release = (): void => {
      if (settled) return;
      settled = true;
      if (generation === this.generation && this.outstanding > 0) this.outstanding -= 1;
    };
    let reply: Promise<unknown>;
    try {
      reply = this.sendPing();
    } catch (error) {
      release();
      this.die(`ping could not be sent: ${error instanceof Error ? error.message : String(error)}`);
      return;
    }
    reply.then(release, release);
  }

  /** The deadline timer re-arms itself for the remaining time, so it fires exactly on silence. */
  private armDeadline(ms: number): void {
    this.deadlineTimer = this.clock.setTimeout(() => {
      if (!this.running) return;
      const silent = this.clock.now() - this.lastInbound;
      if (silent >= this.deadlineMs) {
        this.die(`no frame for ${silent} ms`);
        return;
      }
      this.armDeadline(this.deadlineMs - silent);
    }, ms);
  }

  private die(why: string): void {
    this.stop();
    this.onDead(why);
  }
}
