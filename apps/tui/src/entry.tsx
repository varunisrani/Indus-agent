// The TUI's entry: terminal checks and mode hygiene first, then the link to the agent (by default an
// attach to the profile's daemon over its WebSocket, P17-19; `--local`: the bridge child), the
// dashboard feed when the dashboard started us (P18-09), the app runtime (P12-27) and Ink.
//
// Order matters: truecolor and NODE_ENV are set before React or any colour library loads, the
// mode reset is written before the first frame, and every exit path goes through `Lifecycle`.

import type { ConnectHooks } from "@indus/client";
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
  { RpcProvider },
  { AppRuntime, LinkWatch },
  { App },
  { splitCommand },
  attach,
  dashboardFeed,
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
  import("./app/appRuntime.ts"),
  import("./app/App.tsx"),
  import("./composer/externalEditor.ts"),
  import("./transport/attach.ts"),
  import("./transport/dashboardFeed.ts"),
]);

// Started by the dashboard's Chat tab: its publish URL and credential. The credential leaves the
// environment here, before any child (the bridge, the grant helper) is started.
const feedPlan = dashboardFeed.takeFeedPlan(process.env);

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

// The bridge child. `INDUS_TUI_BRIDGE_COMMAND` may be a program or a program with arguments; the
// bridge arguments are added unless it names them itself.
const bridgeWords = splitCommand(process.env.INDUS_TUI_BRIDGE_COMMAND?.trim() ?? "");
const [bridgeProgram, ...bridgeArgs] = bridgeWords;
const bridgeSpawn =
  bridgeProgram === undefined
    ? {}
    : {
        command: bridgeProgram,
        args: bridgeArgs.includes("tui-bridge")
          ? bridgeArgs
          : [...bridgeArgs, "tui-bridge", "--stdio"],
      };
let bridgePid: number | undefined;
const links = new LinkWatch();
const spawnLocal = (hooks: ConnectHooks) => {
  const transport = spawnStdioTransport({
    ...bridgeSpawn,
    logs: hooks.logs,
    onEvent: hooks.onEvent,
  });
  bridgePid = transport.pid;
  return transport;
};
// Attach by default; `--local` (INDUS_TUI_LOCAL=1) runs the bridge child; an explicit
// INDUS_TUI_GATEWAY_URL is dialled and nothing else. A terminal grant at startup runs the local
// child instead, with a visible line (attach.ts).
const plan = attach.chooseTransport(process.env);
const connect =
  plan.mode === "local"
    ? spawnLocal
    : attach.attachConnector({
        plan,
        ...(plan.mode === "daemon"
          ? {
              runGrant: attach.grantRunner({
                command: plan.command,
                args: plan.args,
                env: attach.helperEnv(process.env),
              }),
              local: spawnLocal,
            }
          : {}),
      });
const client = new IndusClient({
  connect: links.wrap(connect),
  // Cursors drop an event delivered twice across a reconnect (the daemon numbers per run).
  replay: plan.mode !== "local",
});
const feed = feedPlan === null ? null : new dashboardFeed.DashboardFeed(feedPlan);
if (feed !== null) {
  client.subscribe((event) => feed.publish(event));
  feed.start();
}
lifecycle.setBridgeStopper(() => {
  feed?.close();
  void client.close();
  if (bridgePid === undefined) return;
  try {
    process.kill(process.platform === "win32" ? bridgePid : -bridgePid, "SIGTERM");
  } catch {
    // already gone
  }
});

const themePinned = detect.detectFromEnv(process.env) !== null;
let redraw: () => void = () => {};
const runtime = new AppRuntime({
  client,
  links,
  env: process.env,
  attached: plan.mode !== "local",
  themePinned,
  exit: (code, reason) => lifecycle.quit(code, reason),
  redraw: () => redraw(),
  emit: (sequence) => {
    process.stdout.write(sequence);
  },
  suspend: async (run) => {
    const handoff = caps.editorHandoff(capabilities);
    // The editor runs synchronously, so nothing reads our stdin meanwhile.
    process.stdout.write(handoff.before);
    try {
      await run();
    } finally {
      process.stdout.write(handoff.after);
      redraw();
    }
  },
});
runtime.start();

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

const hyperlinks = capabilities.multiplexer === null && caps.terminalFamily(process.env) !== null;
const tree = createElement(
  RpcProvider,
  { rpc: runtime.rpc },
  createElement(App, { runtime, hyperlinks }),
);
const app = ink.render(tree, {
  stdout: output.stream,
  stdin: input.stream as unknown as NodeJS.ReadStream,
  alternateScreen: true,
  // Always interactive: the entry refused to start without a terminal above, and Ink would
  // otherwise draw nothing until exit whenever `CI` is set (a dashboard on a CI runner).
  interactive: true,
  exitOnCtrlC: false,
  maxFps: 60,
  patchConsole: false,
  kittyKeyboard: capabilities.kitty ? { mode: "enabled" } : { mode: "disabled" },
});
// Ink paints again only on a render: after the wipe, render the same tree to repaint at once.
redraw = () => {
  app.clear();
  app.rerender(tree);
};
lifecycle.setUnmount(() => {
  runtime.dispose();
  app.unmount();
});
