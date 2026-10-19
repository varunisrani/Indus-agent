// The TUI's entry: terminal checks and mode hygiene first, then the bridge child, the stores and
// Ink. The full app composition arrives with P12-27; until then a small boot view renders.
//
// Order matters: truecolor and NODE_ENV are set before React or any colour library loads, the
// mode reset is written before the first frame, and every exit path goes through `Lifecycle`.

import { applyTruecolorOptIn } from "./theme/detect.ts";

applyTruecolorOptIn(process.env);
process.env.NODE_ENV ??= "production";

const { refuseWithoutTty, Lifecycle, EXIT_DEAD_TERMINAL, EXIT_MEMORY } = await import(
  "./terminal/lifecycle.ts"
);

if (refuseWithoutTty(process.stdin, (text) => process.stdout.write(text))) process.exit(0);

const { writeModeReset, seq } = await import("./terminal/modes.ts");
// A previous run may have been killed with mouse reporting or the alternate screen still on.
writeModeReset(process.stdout);

const lifecycle = new Lifecycle();
lifecycle.install();

const [
  { IndusClient },
  { spawnStdioTransport },
  ink,
  { createElement },
  { DeadStreamGuard },
  { MemoryMonitor },
  caps,
  { gateStdout },
  { InputFilter },
  { bootTheme, bootCachePath, writeBootTheme },
  detect,
  theme,
  { rpcFromClient, RpcProvider },
  { TurnController },
  { createEventHandler },
  { BootView },
] = await Promise.all([
  import("@indus/client"),
  import("@indus/client/stdio"),
  import("ink"),
  import("react"),
  import("./terminal/deadStream.ts"),
  import("./terminal/memoryMonitor.ts"),
  import("./terminal/capabilities.ts"),
  import("./terminal/outputGate.ts"),
  import("./terminal/inputFilter.ts"),
  import("./theme/bootCache.ts"),
  import("./theme/detect.ts"),
  import("./theme/theme.ts"),
  import("./app/rpc.ts"),
  import("./turn/controller.ts"),
  import("./events/handleEvent.ts"),
  import("./bootView.tsx"),
]);

// The first frame paints the cached theme (no flash); detection refines it after.
const cachePath = bootCachePath(process.env);
bootTheme(process.env, cachePath);

const capabilities = caps.detectCapabilities(process.env);
const deadStream = new DeadStreamGuard({
  onDead: (code) => {
    process.stderr.write(`indus tui: terminal gone (${code}); stopping\n`);
    lifecycle.quit(EXIT_DEAD_TERMINAL, "dead-terminal");
  },
});
deadStream.watch(process.stdout);

const stopMemory = new MemoryMonitor({
  onWarn: (r) =>
    process.stderr.write(`indus tui: heap growing fast (${Math.round(r.used / 2 ** 20)} MiB)\n`),
  onCritical: (r) => {
    process.stderr.write(
      `indus tui: out of memory (${Math.round(r.used / 2 ** 20)} MiB); exiting\n`,
    );
    lifecycle.quit(EXIT_MEMORY, "memory");
  },
}).start();
process.on("beforeExit", stopMemory);

// The bridge child.
const bridgeCommand = process.env.INDUS_TUI_BRIDGE_COMMAND?.trim();
let bridgePid: number | undefined;
const client = new IndusClient({
  connect: (hooks) => {
    const transport = spawnStdioTransport({
      ...(bridgeCommand ? { command: bridgeCommand } : {}),
      logs: hooks.logs,
      onEvent: hooks.onEvent,
    });
    bridgePid = transport.pid;
    return transport;
  },
});
lifecycle.setBridgeStopper(() => {
  void client.close();
  if (bridgePid === undefined) return;
  try {
    process.kill(process.platform === "win32" ? bridgePid : -bridgePid, "SIGTERM");
  } catch {
    // already gone
  }
});

const rpc = rpcFromClient(client);
const controller = new TurnController({ rpc });
const themePinned = detect.detectFromEnv(process.env) !== null;
const handle = createEventHandler({ controller, themePinned });
client.subscribe(handle);
client.start();

// Our wrappers around Ink's streams.
const output = gateStdout(process.stdout, {
  syncOutput: capabilities.syncOutput,
  onWriteError: (error) => deadStream.noteError(error),
  onWriteOk: () => deadStream.noteWrite(),
});
const input = new InputFilter(process.stdin, () => {});

// Light/dark: env first, then one OSC 11 probe; the resolved theme is cached for the next start.
void detect
  .resolveLightMode(process.env, () =>
    detect.probeBackground({ input: process.stdin, output: process.stdout }),
  )
  .then((decision) => {
    theme.setDetected(decision.light, decision.background);
    const resolved = theme.applyThemePayload(
      themePinned ? "auto" : theme.themeStore.getState().theme.name,
    );
    writeBootTheme(cachePath, resolved);
  });

process.stdout.write(
  seq.pasteOn + caps.mouseOn(capabilities.mouse) + (capabilities.otherKeys ? seq.otherKeysOn : ""),
);

const app = ink.render(createElement(RpcProvider, { rpc }, createElement(BootView)), {
  stdout: output.stream,
  stdin: input.stream as unknown as NodeJS.ReadStream,
  alternateScreen: true,
  exitOnCtrlC: false,
  maxFps: 60,
  patchConsole: false,
  kittyKeyboard: capabilities.kitty ? { mode: "enabled" } : { mode: "disabled" },
});
lifecycle.setUnmount(() => app.unmount());
