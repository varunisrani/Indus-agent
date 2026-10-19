// Test-only driver: mounts a component in Ink against fake terminal streams so tests can press
// keys and read the frames Ink paints. Nothing here runs in the product.

import { EventEmitter } from "node:events";
import { render } from "ink";
import type { ReactElement } from "react";

/** Byte sequences a terminal sends for the keys the cards use. */
export const KEY = {
  up: "\u001b[A",
  down: "\u001b[B",
  right: "\u001b[C",
  left: "\u001b[D",
  enter: "\r",
  escape: "\u001b",
  tab: "\t",
  shiftTab: "\u001b[Z",
  backspace: "\u007f",
  space: " ",
  pageUp: "\u001b[5~",
  pageDown: "\u001b[6~",
  ctrl: (letter: string): string => String.fromCharCode(letter.toLowerCase().charCodeAt(0) - 96),
} as const;

class FakeInput extends EventEmitter {
  readonly isTTY = true;
  private readonly chunks: string[] = [];
  setRawMode(): this {
    return this;
  }
  setEncoding(): this {
    return this;
  }
  ref(): this {
    return this;
  }
  unref(): this {
    return this;
  }
  resume(): this {
    return this;
  }
  pause(): this {
    return this;
  }
  read(): string | null {
    return this.chunks.shift() ?? null;
  }
  feed(data: string): void {
    this.chunks.push(data);
    this.emit("readable");
  }
}

class FakeOutput extends EventEmitter {
  readonly isTTY = true;
  readonly writes: string[] = [];
  readonly columns: number;
  readonly rows: number;
  constructor(columns: number, rows: number) {
    super();
    this.columns = columns;
    this.rows = rows;
  }
  write(chunk: string): boolean {
    this.writes.push(chunk);
    return true;
  }
}

export interface Mounted {
  /** Every chunk Ink wrote, ANSI stripped. */
  frames(): string[];
  /** The newest frame, ANSI stripped. */
  lastFrame(): string;
  /** Feed key sequences one at a time, letting React settle after each. */
  press(...keys: string[]): Promise<void>;
  unmount(): void;
}

// biome-ignore lint/suspicious/noControlCharactersInRegex: matching terminal escapes is the point
const ANSI = /\u001b\[[0-9;?<>=]*[ -/]*[@-~]|\u001b\][^\u0007]*\u0007|\u001b[()][0-9A-Za-z]/g;

export const stripAnsi = (text: string): string => text.replace(ANSI, "");

const settle = (ms = 5): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

// Ink holds a lone ESC for 20 ms in case more bytes of a sequence follow.
const ESCAPE_HOLD_MS = 40;

export async function mount(
  element: ReactElement,
  size: { columns?: number; rows?: number } = {},
): Promise<Mounted> {
  const stdin = new FakeInput();
  const stdout = new FakeOutput(size.columns ?? 80, size.rows ?? 30);
  const instance = render(element, {
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await settle();
  const frames = (): string[] => stdout.writes.map(stripAnsi).filter((f) => f.trim() !== "");
  return {
    frames,
    lastFrame: () => frames().at(-1) ?? "",
    press: async (...keys) => {
      for (const key of keys) {
        stdin.feed(key);
        await settle(key === KEY.escape ? ESCAPE_HOLD_MS : 5);
      }
    },
    unmount: () => {
      instance.unmount();
      instance.cleanup();
    },
  };
}
