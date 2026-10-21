#!/usr/bin/env node
// A fake `indus` executable for desktop tests (P20-01 fixture; P20-08..17 point their resolver
// and attach code at it). It answers the commands the desktop runs, with the real output shapes:
//
//   indus --version                          → "indus <version>"
//   indus [-p NAME] gateway ensure --json    → {ws_url, profile_id, instance_id, code_version}
//                                              (attach to or start the fake daemon; a daemon on
//                                              another code version is restarted once, as P17-15)
//   indus [-p NAME] gateway ensure --json --no-start → the same, or exit 1 {state: "absent"}
//   indus [-p NAME] serve ensure --json      → {url, port, pid, profile_id, code_version}
//                                              (token from INDUS_DASHBOARD_SESSION_TOKEN; a serve
//                                              running with another token is replaced)
//   indus [-p NAME] gateway stop | gateway restart | serve stop
//
// Environment: INDUS_HOME (required: the profile home; `-p NAME` → <home>/profiles/NAME),
// INDUS_FAKE_SCENARIO (a name under scenarios/ or a path; default "healthy").

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { controlQuery } from "./control.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function scenarioFile() {
  const raw = process.env.INDUS_FAKE_SCENARIO || "healthy";
  if (raw.includes("/") || raw.endsWith(".json")) return path.resolve(raw);
  return path.join(HERE, "scenarios", `${raw}.json`);
}

const SCENARIO_PATH = scenarioFile();
const scenario = JSON.parse(fs.readFileSync(SCENARIO_PATH, "utf8"));
const VERSION = scenario.version ?? "0.20.0";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function out(value) {
  process.stdout.write(`${typeof value === "string" ? value : JSON.stringify(value)}\n`);
}

function fail(json, state, reason, detail, code = 1) {
  if (json) out({ state, reason, detail });
  else process.stderr.write(`indus: ${detail}\n`);
  return code;
}

function parseArgs(argv) {
  const args = [...argv];
  let profile = null;
  const flags = new Set();
  const words = [];
  while (args.length > 0) {
    const arg = args.shift();
    if (arg === "-p" || arg === "--profile") profile = args.shift() ?? null;
    else if (arg.startsWith("--profile=")) profile = arg.slice("--profile=".length);
    else if (arg.startsWith("-")) flags.add(arg);
    else words.push(arg);
  }
  return { profile, flags, words };
}

function profileHome(profile) {
  const base = process.env.INDUS_HOME;
  if (!base) throw new Error("INDUS_HOME is required by the fake indus");
  const home =
    profile === null || profile === "default" ? base : path.join(base, "profiles", profile);
  fs.mkdirSync(path.join(home, "logs"), { recursive: true, mode: 0o700 });
  return home;
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function startCount(home, role) {
  const file = path.join(home, `fake-${role}-starts.json`);
  const count = readJson(file)?.count ?? 0;
  fs.writeFileSync(file, JSON.stringify({ count: count + 1 }), { mode: 0o600 });
  return count;
}

async function spawnServer(home, role, extraEnv = {}) {
  const stateFile = path.join(home, `fake-${role}.json`);
  fs.rmSync(stateFile, { force: true });
  const log = fs.openSync(path.join(home, "logs", `fake-${role}.log`), "a", 0o600);
  const start = startCount(home, role);
  const child = spawn(
    process.execPath,
    [
      path.join(HERE, "server.mjs"),
      "--role",
      role,
      "--home",
      home,
      "--scenario",
      SCENARIO_PATH,
      "--start",
      String(start),
    ],
    {
      cwd: home,
      detached: true,
      stdio: ["ignore", log, log],
      env: { ...process.env, ...extraEnv },
    },
  );
  child.unref();
  fs.closeSync(log);
  const deadline = Date.now() + (scenario.startTimeoutMs ?? 10_000);
  while (Date.now() < deadline) {
    const state = readJson(stateFile);
    if (state !== null && state.pid === child.pid) return state;
    if (!alive(child.pid)) break;
    await sleep(25);
  }
  return null;
}

async function stopServer(home, role) {
  const state = readJson(path.join(home, `fake-${role}.json`));
  if (state === null || !alive(state.pid)) return false;
  if (role === "daemon") await controlQuery(home, "stop");
  else process.kill(state.pid, "SIGTERM");
  const deadline = Date.now() + 5_000;
  while (alive(state.pid) && Date.now() < deadline) await sleep(25);
  if (alive(state.pid)) process.kill(state.pid, "SIGKILL");
  return true;
}

async function runningDaemon(home) {
  const state = readJson(path.join(home, "fake-daemon.json"));
  if (state === null || !alive(state.pid)) return null;
  const answer = await controlQuery(home, "identify");
  return answer.ok ? answer.result : null;
}

async function gatewayEnsure(home, flags) {
  const json = flags.has("--json");
  const ensure = scenario.ensure ?? {};
  if (ensure.delayMs) await sleep(ensure.delayMs);
  if (ensure.exitCode)
    return fail(
      json,
      ensure.state ?? "failed",
      ensure.reason ?? "scripted",
      ensure.detail ?? "scripted ensure failure",
      ensure.exitCode,
    );
  let daemon = await runningDaemon(home);
  if (daemon !== null && daemon.code_version !== VERSION && !flags.has("--no-start")) {
    await stopServer(home, "daemon");
    daemon = null;
  }
  if (daemon === null) {
    if (flags.has("--no-start"))
      return fail(json, "absent", "absent", "no daemon is running for this profile");
    const started = await spawnServer(home, "daemon");
    if (started === null) return fail(json, "exited", "exited", "the fake daemon did not start");
    daemon = started;
  }
  const descriptor = {
    ws_url: daemon.ws_url,
    profile_id: daemon.profile_id,
    instance_id: daemon.instance_id,
    code_version: daemon.code_version,
  };
  out(json ? descriptor : `${descriptor.ws_url} ${descriptor.code_version}`);
  return 0;
}

async function serveEnsure(home, flags) {
  const json = flags.has("--json");
  const serve = scenario.serve ?? {};
  if (serve.delayMs) await sleep(serve.delayMs);
  if (serve.exitCode)
    return fail(
      json,
      "failed",
      serve.reason ?? "scripted",
      "scripted serve failure",
      serve.exitCode,
    );
  const token = process.env.INDUS_DASHBOARD_SESSION_TOKEN ?? "";
  const tokenHash = createHash("sha256").update(token).digest("hex");
  let state = readJson(path.join(home, "fake-serve.json"));
  if (state !== null && (!alive(state.pid) || state.token_sha256 !== tokenHash)) {
    await stopServer(home, "serve");
    state = null;
  }
  if (state === null) {
    state = await spawnServer(home, "serve", { INDUS_DASHBOARD_SESSION_TOKEN: token });
    if (state === null) return fail(json, "exited", "exited", "the fake serve did not start");
  }
  const answer = {
    url: state.url,
    port: state.port,
    pid: state.pid,
    profile_id: state.profile_id,
    code_version: state.code_version,
  };
  out(json ? answer : answer.url);
  return 0;
}

async function main(argv) {
  const { profile, flags, words } = parseArgs(argv);
  if (flags.has("--version") || words[0] === "version") {
    if (scenario.versionDelayMs) await sleep(scenario.versionDelayMs);
    if (scenario.versionExitCode) return scenario.versionExitCode;
    out(`indus ${VERSION}`);
    return 0;
  }
  const home = profileHome(profile);
  const [group, verb] = words;
  if (group === "gateway" && verb === "ensure") return gatewayEnsure(home, flags);
  if (group === "serve" && verb === "ensure") return serveEnsure(home, flags);
  if (group === "gateway" && verb === "stop") return (await stopServer(home, "daemon")) ? 0 : 1;
  if (group === "gateway" && verb === "restart") {
    await stopServer(home, "daemon");
    return (await spawnServer(home, "daemon")) === null ? 1 : 0;
  }
  if (group === "serve" && verb === "stop") return (await stopServer(home, "serve")) ? 0 : 1;
  process.stderr.write(`fake indus: unsupported command: ${argv.join(" ")}\n`);
  return 2;
}

process.exitCode = await main(process.argv.slice(2));
