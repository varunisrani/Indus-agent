#!/usr/bin/env node
// A stand-in for the P23 installer scripts (P20-09 fixture). It speaks the stage protocol the
// desktop's in-app runner drives (`electron/installer/stageProtocol.ts`):
//
//   fakeInstaller.mjs --manifest [--non-interactive] [--indus-home H]
//       → banner text, a JSON line that is not a manifest, then the manifest JSON line
//   fakeInstaller.mjs --stage NAME --json [--non-interactive] [--indus-home H]
//       → log lines, then one result frame {ok, stage, skipped?, reason?} as the last line
//
// Behaviour per stage comes from a scenario (env INDUS_FAKE_INSTALLER_SCENARIO: a JSON file path or
// inline JSON): { stages: [{name, title?, category?, needs_user_input?, mode?, times?}],
// manifestExit?, protocolVersion? }. Modes:
//   ok          a few log lines and {ok: true}
//   skip        {ok: true, skipped: true, reason}
//   fail        {ok: false, reason: "scripted failure"}
//   die         exit 255 with no result frame (an abrupt stage-host death)
//   die-once    die the first `times` runs (default 1), then ok (count kept in the state dir)
//   hold        start a detached grandchild that keeps stdout open for 60 s, print the frame,
//               exit 0 (the grandchild's pid goes to <state>/holder.pid)
//   cp1252      raw Windows-1252 bytes in a log line before the frame
//   json-noise  an earlier frame-shaped JSON line saying ok:false, then the real ok frame
//   crlf-ansi   CRLF line ends, colour escapes and a carriage-return progress redraw
//   slow        log one line and sleep 30 s before answering (for cancel tests)
// State dir: env INDUS_FAKE_INSTALLER_STATE (counters, pids). Every run appends its argv to
// <state>/runs.jsonl so tests can count stage attempts.

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const DEFAULT_STAGES = [
  { name: "prerequisites", title: "Check prerequisites", category: "system" },
  { name: "runtime", title: "Install the runtime", category: "runtime" },
  { name: "config", title: "Write the configuration", category: "config" },
  {
    name: "setup",
    title: "Provider setup",
    category: "setup",
    needs_user_input: true,
    mode: "skip",
  },
];

function loadScenario() {
  const raw = process.env.INDUS_FAKE_INSTALLER_SCENARIO ?? "";
  if (raw === "") return { stages: DEFAULT_STAGES };
  const text = raw.trim().startsWith("{") ? raw : fs.readFileSync(raw, "utf8");
  return JSON.parse(text);
}

const scenario = loadScenario();
const stateDir = process.env.INDUS_FAKE_INSTALLER_STATE ?? "";
const argv = process.argv.slice(2);

function record() {
  if (stateDir === "") return;
  fs.mkdirSync(stateDir, { recursive: true });
  fs.appendFileSync(path.join(stateDir, "runs.jsonl"), `${JSON.stringify(argv)}\n`);
}

function bump(name) {
  if (stateDir === "") return 0;
  const file = path.join(stateDir, `count-${name}`);
  let count = 0;
  try {
    count = Number(fs.readFileSync(file, "utf8")) || 0;
  } catch {
    // first run
  }
  fs.writeFileSync(file, String(count + 1));
  return count;
}

function line(text) {
  process.stdout.write(`${text}\n`);
}

function frame(value) {
  line(JSON.stringify(value));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runStage(name) {
  const stage = (scenario.stages ?? DEFAULT_STAGES).find((s) => s.name === name);
  if (stage === undefined) {
    frame({ ok: false, stage: name, reason: "unknown stage" });
    return 1;
  }
  const mode = stage.mode ?? "ok";
  line(`==> ${stage.title ?? name}`);
  switch (mode) {
    case "skip":
      frame({ ok: true, stage: name, skipped: true, reason: "non-interactive" });
      return 0;
    case "fail":
      process.stderr.write("something went wrong\n");
      frame({ ok: false, stage: name, reason: "scripted failure" });
      return 1;
    case "die":
      line("the host is about to vanish");
      return 255;
    case "die-once": {
      const seen = bump(name);
      if (seen < (stage.times ?? 1)) {
        line("the host is about to vanish");
        return 255;
      }
      frame({ ok: true, stage: name });
      return 0;
    }
    case "hold": {
      const holder = spawn(process.execPath, ["-e", "setTimeout(() => {}, 60000)"], {
        detached: true,
        stdio: ["ignore", "inherit", "inherit"],
      });
      holder.unref();
      if (stateDir !== "") {
        fs.mkdirSync(stateDir, { recursive: true });
        fs.writeFileSync(path.join(stateDir, "holder.pid"), String(holder.pid));
      }
      frame({ ok: true, stage: name });
      return 0;
    }
    case "cp1252":
      // “quoted” café — in Windows-1252 bytes
      process.stdout.write(
        Buffer.from([
          0x93, 0x71, 0x75, 0x6f, 0x74, 0x65, 0x64, 0x94, 0x20, 0x63, 0x61, 0x66, 0xe9, 0x0a,
        ]),
      );
      frame({ ok: true, stage: name });
      return 0;
    case "json-noise":
      frame({ ok: false, stage: name, reason: "an old frame printed by a child" });
      line("more output");
      frame({ ok: true, stage: name });
      return 0;
    case "crlf-ansi":
      process.stdout.write("\u001b[32mgreen text\u001b[0m\r\n");
      process.stdout.write("progress 10%\rprogress 50%\rprogress 100%\r\n");
      process.stdout.write(`${JSON.stringify({ ok: true, stage: name })}\r\n`);
      return 0;
    case "slow":
      line("working slowly");
      await sleep(30_000);
      frame({ ok: true, stage: name });
      return 0;
    default:
      line(`${name}: step 1`);
      line(`${name}: step 2`);
      frame({ ok: true, stage: name });
      return 0;
  }
}

async function main() {
  record();
  if (argv.includes("--manifest")) {
    line("Indus installer (fake)");
    frame({ note: "not a manifest" });
    if (scenario.manifestExit) return scenario.manifestExit;
    frame({
      protocol_version: scenario.protocolVersion ?? 1,
      stages: (scenario.stages ?? DEFAULT_STAGES).map((s) => ({
        name: s.name,
        title: s.title ?? s.name,
        category: s.category ?? "general",
        needs_user_input: s.needs_user_input === true,
      })),
    });
    return 0;
  }
  const at = argv.indexOf("--stage");
  if (at >= 0 && argv[at + 1] !== undefined) return runStage(argv[at + 1]);
  process.stderr.write("fake installer: nothing to do\n");
  return 2;
}

process.exitCode = await main();
