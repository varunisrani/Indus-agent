// Child process for the exit-path tests: installs the lifecycle with a terminal-like stdout and
// leaves through the path named in argv[2]. Markers on stdout show the order of the steps.

import { DeadStreamGuard } from "../deadStream.ts";
import {
  EXIT_CTRL_C,
  EXIT_MEMORY,
  EXIT_OK,
  EXIT_UPDATE,
  Lifecycle,
  refuseWithoutTty,
} from "../lifecycle.ts";
import { MemoryMonitor } from "../memoryMonitor.ts";

const path = process.argv[2] ?? "";

if (path === "no-tty") {
  if (refuseWithoutTty(process.stdin, (text) => process.stdout.write(text))) process.exit(0);
  process.stdout.write("tty?\n");
  process.exit(3);
}

const life = new Lifecycle({ out: { isTTY: true, fd: 1 }, log: () => {} });
life.install();
life.setUnmount(() => process.stdout.write("<unmount>"));
life.setBridgeStopper(() => process.stdout.write("<bridge-stopped>"));
process.stdout.write("<ready>");

const keepAlive = setTimeout(() => process.stdout.write("<timeout>"), 5_000);

switch (path) {
  case "exit":
    clearTimeout(keepAlive);
    process.exit(EXIT_OK);
    break;
  case "sigterm":
    process.kill(process.pid, "SIGTERM");
    break;
  case "sighup":
    process.kill(process.pid, "SIGHUP");
    break;
  case "uncaught":
    setTimeout(() => {
      throw new Error("planted failure");
    }, 0);
    break;
  case "rejection":
    void Promise.reject(new Error("planted rejection"));
    break;
  case "quit":
    life.quit(EXIT_OK, "quit");
    break;
  case "ctrl-d":
    life.quit(EXIT_OK, "ctrl-d");
    break;
  case "ctrl-c":
    life.quit(EXIT_CTRL_C, "ctrl-c");
    break;
  case "update":
    life.quit(EXIT_UPDATE, "update");
    break;
  case "memory":
    new MemoryMonitor({
      read: () => ({ used: 97, limit: 100 }),
      onWarn: () => {},
      onCritical: () => life.quit(EXIT_MEMORY, "memory"),
    }).check();
    break;
  case "dead": {
    const guard = new DeadStreamGuard({ onDead: () => life.quit(1, "dead-terminal") });
    for (let i = 0; i < 5; i++)
      guard.noteError(Object.assign(new Error("write EPIPE"), { code: "EPIPE" }));
    break;
  }
  default:
    process.stdout.write(`<unknown path ${path}>`);
    process.exit(9);
}
