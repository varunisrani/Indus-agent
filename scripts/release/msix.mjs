#!/usr/bin/env node
// MSIX packaging for the Windows desktop app (P24-22; docs/notes/P24/msix.md).
//
//   node scripts/release/msix.mjs build --version X.Y.Z[-canary.STAMP] [--arch x64,arm64]
//        [--unpacked-x64 DIR] [--unpacked-arm64 DIR] [--out DIR] [--store]
//   node scripts/release/msix.mjs render --version ... [--arch x64] [--store]
//
// `build` runs on a Windows release runner after electron-builder has left the unpacked apps
// (`win-unpacked`, `win-arm64-unpacked`). For each architecture it lays the app out with a rendered
// AppxManifest.xml and tile images and packs one .msix; the packages go into one .msixbundle; an
// App Installer file points installed copies at that bundle on GitHub Releases. Signing happens only
// when the signing secrets are present (by name, see SIGNING); otherwise the bundle is unsigned and
// one line says so. `--store` adds a separate, never-signed bundle with the Store identity, for
// stable releases only. Canary builds have their own identity and can never carry the Store one.
// `render` prints one validated manifest, for review on any OS.
//
// Everything that touches Windows tools goes through `deps.exec`, so the tests drive the whole flow
// with recorders; nothing here reads a secret value except to hand it to signtool.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PACKAGING = path.join(ROOT, "apps", "desktop", "packaging", "msix");
export const TEMPLATE_FILE = path.join(PACKAGING, "AppxManifest.template.xml");
export const IDENTITY_FILE = path.join(PACKAGING, "identity.json");
const VERSIONS_FILE = path.join(ROOT, "src", "indus", "foundation", "versions.py");
const ICONS = path.join(ROOT, "apps", "desktop", "resources", "icons", "png");
/** electron-builder's output: the unpacked apps come from here and the packages go back here. */
const RELEASE_DIR = path.join(ROOT, "apps", "desktop", "build", "release");

/** Windows 11 22H2 is the oldest Windows the package installs on; 24H2 is the newest we test. */
export const MIN_WINDOWS = "10.0.22621.0";
export const MAX_WINDOWS_TESTED = "10.0.26100.0";
export const ARCHES = Object.freeze(["x64", "arm64"]);
export const PLACEHOLDER = "OWNER-TO-FILL";
/** Canary quads count seconds from here (W49-d-1). */
const CANARY_ORIGIN = Date.UTC(2026, 0, 1) / 1000;
const SIGN_ATTEMPTS = 3;
const FEED_HOURS = 12;

const NS = Object.freeze({
  foundation: "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
  uap: "http://schemas.microsoft.com/appx/manifest/uap/windows10",
  rescap: "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
  desktop6: "http://schemas.microsoft.com/appx/manifest/desktop/windows10/6",
  appInstaller: "http://schemas.microsoft.com/appx/appinstaller/2021",
});

export class MsixError extends Error {}

const fail = (message) => {
  throw new MsixError(message);
};

// ── versions ────────────────────────────────────────────────────────────────────────────────

const RELEASE_RE =
  /^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-canary\.([0-9]{8}T[0-9]{6}Z))?$/;

/** Unix seconds of a canary stamp (`YYYYMMDDTHHMMSSZ`); an impossible date is refused. */
export function stampSeconds(stamp) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp);
  if (!m) fail(`canary stamp ${JSON.stringify(stamp)} is not YYYYMMDDTHHMMSSZ`);
  const [year, month, day, hour, minute, second] = m.slice(1).map(Number);
  const ms = Date.UTC(year, month - 1, day, hour, minute, second);
  const back = new Date(ms);
  const same =
    back.getUTCFullYear() === year &&
    back.getUTCMonth() === month - 1 &&
    back.getUTCDate() === day &&
    back.getUTCHours() === hour &&
    back.getUTCMinutes() === minute &&
    back.getUTCSeconds() === second;
  if (!same) fail(`canary stamp ${stamp} is not a real UTC time`);
  return ms / 1000;
}

/** `X.Y.Z` or `X.Y.Z-canary.STAMP` (the release grammar of indus.foundation.versions). */
export function parseRelease(text) {
  const m = RELEASE_RE.exec(String(text ?? ""));
  if (!m) fail(`${JSON.stringify(text)} is not a release version (X.Y.Z or X.Y.Z-canary.STAMP)`);
  const [major, minor, patch] = m.slice(1, 4).map(Number);
  const stamp = m[4] ?? null;
  if (stamp !== null) stampSeconds(stamp);
  return { text: m[0], major, minor, patch, stamp, channel: stamp === null ? "stable" : "canary" };
}

const sixteenBits = (n) => Number.isInteger(n) && n >= 0 && n <= 0xffff;

/** The four-part package version Windows compares (W49-d-1). */
export function packageVersion(release) {
  const r = typeof release === "string" ? parseRelease(release) : release;
  let third = r.patch;
  let fourth = 0;
  if (r.channel === "canary") {
    const since = stampSeconds(r.stamp) - CANARY_ORIGIN;
    if (since < 0) fail(`canary stamp ${r.stamp} is before 2026-01-01`);
    third = Math.floor(since / 0x10000);
    fourth = since % 0x10000;
  }
  const quad = [r.major, r.minor, third, fourth];
  if (!quad.every(sixteenBits)) fail(`${r.text} does not fit four 16-bit fields`);
  return quad.join(".");
}

/** Numeric order of two quads (negative, zero, positive). */
export function compareQuads(a, b) {
  const x = a.split(".").map(Number);
  const y = b.split(".").map(Number);
  for (let i = 0; i < 4; i += 1) {
    if (x[i] !== y[i]) return (x[i] ?? 0) - (y[i] ?? 0);
  }
  return 0;
}

// ── identities ──────────────────────────────────────────────────────────────────────────────

const IDENTITY_KEYS = [
  "identityName",
  "displayName",
  "publisher",
  "publisherDisplayName",
  "protocol",
  "filePrefix",
];

export function loadIdentities(file = IDENTITY_FILE) {
  const raw = JSON.parse(fs.readFileSync(file, "utf8"));
  for (const kind of ["stable", "canary", "store"]) {
    const entry = raw[kind];
    if (!entry || typeof entry !== "object") fail(`${file}: no "${kind}" identity`);
    for (const key of IDENTITY_KEYS) {
      if (typeof entry[key] !== "string" || entry[key] === "")
        fail(`${file}: ${kind}.${key} missing`);
    }
  }
  return raw;
}

export const isPlaceholder = (value) => String(value).includes(PLACEHOLDER);

/**
 * The identity one package is built with. The Store identity belongs to stable releases only, and
 * a canary identity that shares a name or publisher with the Store one is refused outright.
 */
export function identityFor(identities, { channel, store = false }) {
  if (channel !== "stable" && channel !== "canary") fail(`unknown channel ${channel}`);
  const storeEntry = identities.store;
  if (store) {
    if (channel !== "stable") fail("the Store bundle is for stable releases only");
    return { kind: "store", ...storeEntry };
  }
  const entry = identities[channel];
  if (channel === "canary") {
    if (entry.identityName === identities.stable.identityName)
      fail("the canary identity must differ from the stable one");
    if (entry.identityName === storeEntry.identityName || entry.publisher === storeEntry.publisher)
      fail("a canary package may never use the Store identity");
  }
  return { kind: channel, ...entry };
}

// ── manifest ────────────────────────────────────────────────────────────────────────────────

const escapeXml = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");

/** Fill `{{key}}` fields; a field without a value, or a value never used, is an error. */
export function fillTemplate(template, values) {
  const used = new Set();
  const out = template.replace(/\{\{([A-Za-z][A-Za-z0-9]*)\}\}/g, (_, key) => {
    if (!Object.hasOwn(values, key)) fail(`template field {{${key}}} has no value`);
    used.add(key);
    return escapeXml(values[key]);
  });
  if (out.includes("{{") || out.includes("}}")) fail("template has a malformed {{field}}");
  const unused = Object.keys(values).filter((key) => !used.has(key));
  if (unused.length > 0) fail(`template never uses: ${unused.join(", ")}`);
  return out;
}

export function manifestValues(identity, quad, arch) {
  if (!ARCHES.includes(arch)) fail(`unsupported architecture ${arch} (${ARCHES.join(", ")})`);
  return {
    identityName: identity.identityName,
    publisher: identity.publisher,
    version: quad,
    arch,
    displayName: identity.displayName,
    publisherDisplayName: identity.publisherDisplayName,
    description: "Chat with Indus Agent, review its edits and run its tools from a desktop window.",
    minVersion: MIN_WINDOWS,
    maxVersionTested: MAX_WINDOWS_TESTED,
    applicationId: "Indus",
    executable: identity.executable ?? "Indus.exe",
    protocol: identity.protocol,
  };
}

/** Render and check one manifest; a manifest that breaks a schema rule never leaves here. */
export function renderManifest({ template, identity, quad, arch }) {
  const xml = fillTemplate(template, manifestValues(identity, quad, arch));
  const problems = checkManifest(xml);
  if (problems.length > 0) fail(`rendered manifest is invalid:\n  ${problems.join("\n  ")}`);
  return xml;
}

// ── a small XML reader (enough for the two documents we write) ──────────────────────────────

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

function decodeText(raw, where) {
  return raw.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-z]+);|&/g, (_whole, ref) => {
    if (ref === undefined) fail(`stray '&' in ${where}`);
    if (ref.startsWith("#x")) return String.fromCodePoint(Number.parseInt(ref.slice(2), 16));
    if (ref.startsWith("#")) return String.fromCodePoint(Number.parseInt(ref.slice(1), 10));
    if (!Object.hasOwn(ENTITIES, ref)) fail(`unknown entity &${ref}; in ${where}`);
    return ENTITIES[ref];
  });
}

/**
 * Parse a document into `{ name, attrs, children, text }` nodes. Refuses what our files never
 * contain (DOCTYPE, CDATA, processing instructions after the declaration) instead of guessing.
 */
export function parseXml(source) {
  let pos = 0;
  const text = String(source);
  const rest = () => text.slice(pos);
  const skipMisc = () => {
    for (;;) {
      const ws = /^\s+/.exec(rest());
      if (ws) pos += ws[0].length;
      if (text.startsWith("<!--", pos)) {
        const end = text.indexOf("-->", pos + 4);
        if (end < 0) fail("unterminated comment");
        if (text.slice(pos + 4, end).includes("--")) fail("'--' inside a comment");
        pos = end + 3;
        continue;
      }
      return;
    }
  };
  if (text.startsWith("<?xml")) {
    const end = text.indexOf("?>");
    if (end < 0) fail("unterminated XML declaration");
    pos = end + 2;
  }
  skipMisc();
  if (text.startsWith("<!", pos) || text.startsWith("<?", pos)) fail("DOCTYPE or PI not allowed");

  const NAME = /^[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?/;
  const readElement = () => {
    if (text[pos] !== "<") fail(`expected an element at offset ${pos}`);
    pos += 1;
    const nameMatch = NAME.exec(rest());
    if (!nameMatch) fail(`bad element name at offset ${pos}`);
    const node = { name: nameMatch[0], attrs: {}, children: [], text: "" };
    pos += nameMatch[0].length;
    for (;;) {
      const ws = /^\s*/.exec(rest());
      pos += ws ? ws[0].length : 0;
      if (text.startsWith("/>", pos)) {
        pos += 2;
        return node;
      }
      if (text[pos] === ">") {
        pos += 1;
        break;
      }
      const attr = /^([A-Za-z_][\w.:-]*)\s*=\s*("([^"<]*)"|'([^'<]*)')/.exec(rest());
      if (!attr) fail(`bad attribute in <${node.name}> at offset ${pos}`);
      if (Object.hasOwn(node.attrs, attr[1]))
        fail(`duplicate attribute ${attr[1]} in <${node.name}>`);
      node.attrs[attr[1]] = decodeText(attr[3] ?? attr[4] ?? "", `<${node.name}> ${attr[1]}`);
      pos += attr[0].length;
    }
    for (;;) {
      const lt = text.indexOf("<", pos);
      if (lt < 0) fail(`<${node.name}> is never closed`);
      node.text += decodeText(text.slice(pos, lt), `<${node.name}>`);
      pos = lt;
      if (text.startsWith("<!--", pos)) {
        skipMisc();
        continue;
      }
      if (text.startsWith("<![CDATA[", pos) || text.startsWith("<?", pos)) {
        fail(`CDATA or PI inside <${node.name}>`);
      }
      if (text.startsWith("</", pos)) {
        const close = /^<\/([^\s>]+)\s*>/.exec(rest());
        if (!close || close[1] !== node.name) fail(`<${node.name}> closed by ${close?.[0] ?? "?"}`);
        pos += close[0].length;
        node.text = node.text.trim();
        return node;
      }
      node.children.push(readElement());
    }
  };
  const root = readElement();
  skipMisc();
  if (pos !== text.length) fail("content after the root element");
  return root;
}

// ── schema rules, checked in code (W49-d-4) ─────────────────────────────────────────────────

const QUAD_RE =
  /^(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})$/;
const isQuad = (v) => QUAD_RE.test(v ?? "") && v.split(".").every((p) => Number(p) <= 0xffff);
const PACKAGE_NAME_RE = /^[-.A-Za-z0-9]{3,50}$/;
const RESERVED_NAME_RE = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$/i;
const DN_KEY =
  "(?:CN|L|O|OU|E|C|S|STREET|T|G|I|SN|DC|SERIALNUMBER|OID\\.(?:0|[1-9]\\d*)(?:\\.(?:0|[1-9]\\d*))+)";
const DN_VALUE = '(?:[^,+="<>#;]+|"[^"]*")';
const PUBLISHER_RE = new RegExp(`^${DN_KEY}=${DN_VALUE}(?:, ${DN_KEY}=${DN_VALUE})*$`);
const APP_ID_RE = /^[A-Za-z][A-Za-z0-9]*(\.[A-Za-z][A-Za-z0-9]*)*$/;
const PROTOCOL_RE = /^[a-z0-9][a-z0-9.+-]{1,38}$/;
const RESERVED_SCHEMES = new Set(["http", "https", "file", "ftp", "mailto", "javascript", "shell"]);
const ARCH_VALUES = new Set(["x86", "x64", "arm", "arm64", "neutral"]);
const PACKAGE_ORDER = [
  "Identity",
  "Properties",
  "Dependencies",
  "Resources",
  "Applications",
  "Capabilities",
];

const childrenNamed = (node, name) => (node?.children ?? []).filter((c) => c.name === name);
const only = (node, name, problems, where) => {
  const found = childrenNamed(node, name);
  if (found.length !== 1) problems.push(`${where}: expected one <${name}>, found ${found.length}`);
  return found[0];
};
const relativeFile = (value, extensions) =>
  typeof value === "string" &&
  value.length > 0 &&
  !/^[\\/]|^[A-Za-z]:|(^|[\\/])\.\.([\\/]|$)/.test(value) &&
  extensions.some((ext) => value.toLowerCase().endsWith(ext));

/** Problems with a rendered package manifest (empty: valid). */
export function checkManifest(xml) {
  const problems = [];
  if (/\{\{|\}\}/.test(xml)) problems.push("an unfilled {{field}} is left");
  let root;
  try {
    root = parseXml(xml);
  } catch (error) {
    return [...problems, `not well-formed XML: ${error.message}`];
  }
  if (root.name !== "Package") return [...problems, `root is <${root.name}>, expected <Package>`];
  if (root.attrs.xmlns !== NS.foundation)
    problems.push("Package is not in the foundation namespace");
  const declared = new Map(
    Object.entries(root.attrs)
      .filter(([k]) => k.startsWith("xmlns:"))
      .map(([k, v]) => [k.slice(6), v]),
  );
  for (const prefix of (root.attrs.IgnorableNamespaces ?? "").split(/\s+/).filter(Boolean)) {
    if (!declared.has(prefix))
      problems.push(`IgnorableNamespaces names undeclared prefix ${prefix}`);
  }
  for (const [prefix, uri] of [
    ["uap", NS.uap],
    ["rescap", NS.rescap],
    ["desktop6", NS.desktop6],
  ]) {
    if (declared.has(prefix) && declared.get(prefix) !== uri)
      problems.push(`xmlns:${prefix} is not ${uri}`);
  }
  const usedPrefixes = new Set();
  const walk = (node) => {
    const colon = node.name.indexOf(":");
    if (colon > 0) usedPrefixes.add(node.name.slice(0, colon));
    node.children.forEach(walk);
  };
  walk(root);
  for (const prefix of usedPrefixes) {
    if (!declared.has(prefix)) problems.push(`element prefix ${prefix} is not declared`);
  }
  const order = root.children.map((c) => c.name);
  const expected = PACKAGE_ORDER.filter((name) => order.includes(name));
  if (order.join() !== expected.join())
    problems.push(`Package children out of order: ${order.join(", ")}`);

  const identity = only(root, "Identity", problems, "Package");
  if (identity) {
    const { Name, Publisher, Version, ProcessorArchitecture } = identity.attrs;
    if (!PACKAGE_NAME_RE.test(Name ?? "") || RESERVED_NAME_RE.test(Name ?? ""))
      problems.push(`Identity Name ${JSON.stringify(Name)} is not a valid package name`);
    if (!PUBLISHER_RE.test(Publisher ?? "") || (Publisher ?? "").length > 8192)
      problems.push(`Identity Publisher ${JSON.stringify(Publisher)} is not a distinguished name`);
    if (!isQuad(Version))
      problems.push(`Identity Version ${JSON.stringify(Version)} is not a quad`);
    if (!ARCH_VALUES.has(ProcessorArchitecture ?? ""))
      problems.push(`ProcessorArchitecture ${JSON.stringify(ProcessorArchitecture)} is unknown`);
  }

  const props = only(root, "Properties", problems, "Package");
  if (props) {
    for (const name of ["DisplayName", "PublisherDisplayName"]) {
      const node = only(props, name, problems, "Properties");
      if (node && (node.text.length < 1 || node.text.length > 256))
        problems.push(`${name} must be 1-256 characters`);
    }
    const logo = only(props, "Logo", problems, "Properties");
    if (logo && !relativeFile(logo.text, [".png", ".jpg", ".jpeg"]))
      problems.push(`Logo ${JSON.stringify(logo.text)} is not a package-relative image`);
    const description = childrenNamed(props, "Description")[0];
    if (description && description.text.length > 2048)
      problems.push("Description over 2048 characters");
    const propOrder = props.children.map((c) => c.name);
    const want = ["DisplayName", "PublisherDisplayName", "Logo", "Description"].filter((n) =>
      propOrder.includes(n),
    );
    if (propOrder.slice(0, want.length).join() !== want.join())
      problems.push(`Properties children out of order: ${propOrder.join(", ")}`);
  }

  const deps = only(root, "Dependencies", problems, "Package");
  const families = childrenNamed(deps, "TargetDeviceFamily");
  const desktop = families.find((f) => f.attrs.Name === "Windows.Desktop");
  if (!desktop) problems.push("no TargetDeviceFamily Windows.Desktop");
  else {
    const { MinVersion, MaxVersionTested } = desktop.attrs;
    if (!isQuad(MinVersion) || compareQuads(MinVersion, MIN_WINDOWS) < 0)
      problems.push(`MinVersion ${MinVersion} is below Windows 11 22H2 (${MIN_WINDOWS})`);
    if (
      !isQuad(MaxVersionTested) ||
      (isQuad(MinVersion) && compareQuads(MaxVersionTested, MinVersion) < 0)
    )
      problems.push(`MaxVersionTested ${MaxVersionTested} is below MinVersion`);
  }

  const resources = only(root, "Resources", problems, "Package");
  const languages = childrenNamed(resources, "Resource").filter((r) => r.attrs.Language);
  if (languages.length === 0) problems.push("Resources names no Language");

  const capabilities = childrenNamed(root, "Capabilities")[0];
  const capNames = new Set();
  let seenDevice = false;
  for (const cap of capabilities?.children ?? []) {
    if (cap.name === "DeviceCapability") seenDevice = true;
    else if (seenDevice) problems.push(`<${cap.name}> after a DeviceCapability`);
    capNames.add(`${cap.name}:${cap.attrs.Name}`);
  }

  const apps = childrenNamed(only(root, "Applications", problems, "Package"), "Application");
  if (apps.length < 1 || apps.length > 100)
    problems.push("Applications must hold 1-100 Application");
  for (const app of apps) {
    const { Id, Executable, EntryPoint } = app.attrs;
    if (!APP_ID_RE.test(Id ?? "") || (Id ?? "").length > 64)
      problems.push(`Application Id ${Id} invalid`);
    if (!relativeFile(Executable, [".exe"]))
      problems.push(`Executable ${Executable} is not a relative .exe`);
    if (
      EntryPoint === "Windows.FullTrustApplication" &&
      !capNames.has("rescap:Capability:runFullTrust")
    )
      problems.push("a full-trust application needs the runFullTrust capability");
    const visual = only(app, "uap:VisualElements", problems, `Application ${Id}`);
    for (const attr of [
      "DisplayName",
      "Description",
      "BackgroundColor",
      "Square150x150Logo",
      "Square44x44Logo",
    ]) {
      if (visual && !visual.attrs[attr]) problems.push(`uap:VisualElements lacks ${attr}`);
    }
    for (const ext of childrenNamed(childrenNamed(app, "Extensions")[0], "uap:Extension")) {
      for (const proto of childrenNamed(ext, "uap:Protocol")) {
        const name = proto.attrs.Name ?? "";
        if (!PROTOCOL_RE.test(name) || RESERVED_SCHEMES.has(name) || name.startsWith("ms-"))
          problems.push(`protocol ${JSON.stringify(name)} is not allowed`);
      }
    }
  }

  const virtualized = (props?.children ?? []).filter((c) =>
    /^desktop6:\w+WriteVirtualization$/.test(c.name),
  );
  for (const node of virtualized) {
    if (node.text !== "disabled" && node.text !== "enabled")
      problems.push(`${node.name} must be enabled|disabled`);
  }
  if (
    virtualized.some((n) => n.text === "disabled") &&
    !capNames.has("rescap:Capability:unvirtualizedResources")
  )
    problems.push("turning write virtualization off needs the unvirtualizedResources capability");
  return problems;
}

/** Problems with a rendered App Installer file (empty: valid). */
export function checkFeed(xml) {
  const problems = [];
  let root;
  try {
    root = parseXml(xml);
  } catch (error) {
    return [`not well-formed XML: ${error.message}`];
  }
  const https = (v) => /^https:\/\/[^\s"<>]+$/.test(v ?? "");
  if (root.name !== "AppInstaller") return [`root is <${root.name}>, expected <AppInstaller>`];
  if (root.attrs.xmlns !== NS.appInstaller)
    problems.push("AppInstaller is not in the 2021 namespace");
  if (!https(root.attrs.Uri)) problems.push("AppInstaller Uri must be https");
  if (!isQuad(root.attrs.Version)) problems.push("AppInstaller Version is not a quad");
  const main = only(root, "MainBundle", problems, "AppInstaller");
  if (main) {
    if (!PACKAGE_NAME_RE.test(main.attrs.Name ?? "")) problems.push("MainBundle Name invalid");
    if (!PUBLISHER_RE.test(main.attrs.Publisher ?? ""))
      problems.push("MainBundle Publisher invalid");
    if (!isQuad(main.attrs.Version)) problems.push("MainBundle Version is not a quad");
    if (!https(main.attrs.Uri)) problems.push("MainBundle Uri must be https");
  }
  const settings = only(root, "UpdateSettings", problems, "AppInstaller");
  const onLaunch = childrenNamed(settings, "OnLaunch")[0];
  const hours = Number(onLaunch?.attrs.HoursBetweenUpdateChecks ?? "24");
  if (!onLaunch) problems.push("UpdateSettings has no OnLaunch");
  else if (!Number.isInteger(hours) || hours < 0 || hours > 255)
    problems.push("HoursBetweenUpdateChecks must be 0-255");
  return problems;
}

// ── App Installer feed ──────────────────────────────────────────────────────────────────────

/** `owner/name` of the release base, read from the one place it is defined (D-16.690). */
export function releaseRepository(file = VERSIONS_FILE) {
  const m = /^RELEASE_REPOSITORY\s*=\s*"([\w.-]+\/[\w.-]+)"/m.exec(fs.readFileSync(file, "utf8"));
  if (!m) fail(`${file}: RELEASE_REPOSITORY not found`);
  return m[1];
}

/** Where the feed and the bundle live on GitHub Releases (W49-d-7). */
export function feedLocations({ repository, release, identity, bundleName }) {
  const base = `https://github.com/${repository}/releases`;
  const feedName = identity.feedName;
  const feed =
    release.channel === "stable"
      ? `${base}/latest/download/${feedName}`
      : `${base}/download/channels/${feedName}`;
  return { feed, bundle: `${base}/download/v${release.text}/${bundleName}` };
}

export function renderFeed({ identity, quad, feedUrl, bundleUrl }) {
  const a = (v) => escapeXml(v);
  const xml = [
    '<?xml version="1.0" encoding="utf-8"?>',
    `<AppInstaller xmlns="${NS.appInstaller}" Version="${a(quad)}" Uri="${a(feedUrl)}">`,
    `  <MainBundle Name="${a(identity.identityName)}" Publisher="${a(identity.publisher)}" Version="${a(quad)}" Uri="${a(bundleUrl)}" />`,
    "  <UpdateSettings>",
    `    <OnLaunch HoursBetweenUpdateChecks="${FEED_HOURS}" ShowPrompt="false" UpdateBlocksActivation="false" />`,
    "    <AutomaticBackgroundTask />",
    "  </UpdateSettings>",
    "</AppInstaller>",
    "",
  ].join("\n");
  const problems = checkFeed(xml);
  if (problems.length > 0) fail(`rendered feed is invalid:\n  ${problems.join("\n  ")}`);
  return xml;
}

// ── signing (secrets by name only) ──────────────────────────────────────────────────────────

export const SIGNING = Object.freeze({
  azure: Object.freeze([
    "AZURE_TRUSTED_SIGNING_ENDPOINT",
    "AZURE_TRUSTED_SIGNING_ACCOUNT",
    "AZURE_TRUSTED_SIGNING_PROFILE",
    "INDUS_AZURE_SIGNING_DLIB",
  ]),
  pfx: Object.freeze(["WIN_CERTIFICATE", "WIN_CERTIFICATE_PASSWORD"]),
});
const TIMESTAMP = Object.freeze({
  azure: "http://timestamp.acs.microsoft.com",
  pfx: "http://timestamp.digicert.com",
});

/**
 * Which signer the environment selects: Azure Trusted Signing first, then a PFX, else none. A group
 * that is only partly set is an error, so a missing secret never turns into a quiet unsigned build.
 */
export function signingPlan(env) {
  const present = (name) => typeof env[name] === "string" && env[name] !== "";
  for (const kind of ["azure", "pfx"]) {
    const names = SIGNING[kind];
    const have = names.filter(present);
    if (have.length === names.length) {
      return { kind, timestampUrl: env.INDUS_SIGN_TIMESTAMP_URL || TIMESTAMP[kind] };
    }
    if (have.length > 0) {
      const missing = names.filter((n) => !present(n));
      fail(`signing (${kind}) is partly configured: ${missing.join(", ")} not set`);
    }
  }
  return { kind: "none" };
}

/** signtool arguments for one file; `secretFile` is the PFX path or the Azure metadata file. */
export function signArgs(plan, env, secretFile, target) {
  const common = ["sign", "/fd", "SHA256", "/td", "SHA256", "/tr", plan.timestampUrl];
  if (plan.kind === "azure") {
    return [...common, "/dlib", env.INDUS_AZURE_SIGNING_DLIB, "/dmdf", secretFile, target];
  }
  return [...common, "/f", secretFile, "/p", env.WIN_CERTIFICATE_PASSWORD, target];
}

/** A command line safe to print: the certificate password never appears. */
export function printable(command, args, env) {
  const secret = env.WIN_CERTIFICATE_PASSWORD;
  const shown = args.map((arg) => (secret && arg === secret ? "***" : arg));
  return [path.basename(command), ...shown].join(" ");
}

// ── build ───────────────────────────────────────────────────────────────────────────────────

/**
 * @typedef {object} Deps
 * @property {Record<string, string | undefined>} env
 * @property {(file: string, args: string[]) => void} exec
 * @property {(line: string) => void} log
 */

/** @returns {Deps} */
const defaultDeps = () => ({
  env: process.env,
  exec: (file, args) => {
    execFileSync(file, args, { stdio: "inherit" });
  },
  log: (line) => {
    process.stderr.write(`[msix] ${line}\n`);
  },
});

/** Copy the tile images the manifest names into a layout's Assets folder. */
function writeAssets(layout, iconsDir) {
  const assets = path.join(layout, "Assets");
  fs.mkdirSync(assets, { recursive: true });
  for (const [target, source] of [
    ["StoreLogo.png", "64x64.png"],
    ["Square44x44Logo.png", "48x48.png"],
    ["Square150x150Logo.png", "256x256.png"],
  ]) {
    const from = path.join(iconsDir, source);
    if (!fs.existsSync(from)) fail(`icon ${from} is missing`);
    fs.copyFileSync(from, path.join(assets, target));
  }
}

/**
 * Build the packages. `options`: version, arches, unpacked {arch: dir}, out, store, template,
 * identities, repository, iconsDir. `deps`: env, exec(file, args), log(line).
 * @param {Record<string, any>} options
 * @param {Deps} [deps]
 */
export function build(options, deps = defaultDeps()) {
  const { env, exec, log } = deps;
  const release = parseRelease(options.version);
  const quad = packageVersion(release);
  const arches = options.arches ?? [...ARCHES];
  if (arches.length === 0 || arches.some((a) => !ARCHES.includes(a)))
    fail(`--arch must name some of ${ARCHES.join(", ")}`);
  const identities = options.identities ?? loadIdentities();
  const identity = identityFor(identities, { channel: release.channel });
  const storeIdentity = options.store
    ? identityFor(identities, { channel: release.channel, store: true })
    : null;
  if (storeIdentity) {
    if (release.major < 1) fail("the Store bundle needs a 1.0.0 or later version");
    for (const key of ["identityName", "publisher", "publisherDisplayName"]) {
      if (isPlaceholder(storeIdentity[key]))
        fail(`Store identity ${key} is still a placeholder (owner item; ${IDENTITY_FILE})`);
    }
  }
  const plan = signingPlan(env);
  if (plan.kind !== "none" && isPlaceholder(identity.publisher))
    fail(
      `signing needs the certificate subject as publisher; ${identity.kind}.publisher is a placeholder`,
    );
  if (plan.kind === "none")
    log("signing secrets not set: the packages and the bundle are unsigned");

  const template = options.template ?? fs.readFileSync(TEMPLATE_FILE, "utf8");
  const out = path.resolve(options.out);
  const work = path.join(out, ".msix-work");
  const makeappx = env.INDUS_MAKEAPPX || "makeappx";
  const signtool = env.INDUS_SIGNTOOL || "signtool";
  const run = (command, args) => {
    log(`run ${printable(command, args, env)}`);
    exec(command, args);
  };
  for (const arch of arches) {
    const dir = options.unpacked?.[arch];
    if (!dir || !fs.existsSync(dir)) fail(`unpacked app for ${arch} not found: ${dir}`);
    const exe = manifestValues(identity, quad, arch).executable;
    if (!fs.existsSync(path.join(dir, exe))) fail(`${dir} has no ${exe}`);
  }

  fs.rmSync(work, { recursive: true, force: true });
  fs.mkdirSync(work, { recursive: true });
  fs.mkdirSync(out, { recursive: true });
  const files = [];
  try {
    let secretFile = null;
    if (plan.kind === "pfx") {
      secretFile = path.join(work, "signing.pfx");
      fs.writeFileSync(secretFile, Buffer.from(env.WIN_CERTIFICATE, "base64"), { mode: 0o600 });
    } else if (plan.kind === "azure") {
      secretFile = path.join(work, "signing-metadata.json");
      const metadata = {
        Endpoint: env.AZURE_TRUSTED_SIGNING_ENDPOINT,
        CodeSigningAccountName: env.AZURE_TRUSTED_SIGNING_ACCOUNT,
        CertificateProfileName: env.AZURE_TRUSTED_SIGNING_PROFILE,
      };
      fs.writeFileSync(secretFile, JSON.stringify(metadata), { mode: 0o600 });
    }
    const sign = (target) => {
      for (let attempt = 1; ; attempt += 1) {
        try {
          run(signtool, signArgs(plan, env, secretFile, target));
          return;
        } catch (error) {
          if (attempt >= SIGN_ATTEMPTS) throw error;
          log(`signing ${path.basename(target)} failed (attempt ${attempt}), trying again`);
        }
      }
    };

    /** Lay out, pack and (maybe) sign one package per arch; then bundle only those packages. */
    const packAndBundle = (who, signed) => {
      const packages = [];
      for (const arch of arches) {
        const layout = path.join(work, `${who.kind}-${arch}`);
        fs.cpSync(options.unpacked[arch], layout, { recursive: true });
        writeAssets(layout, options.iconsDir ?? ICONS);
        const manifest = renderManifest({ template, identity: who, quad, arch });
        fs.writeFileSync(path.join(layout, "AppxManifest.xml"), manifest);
        const msix = path.join(out, `${who.filePrefix}-${quad}-${arch}.msix`);
        run(makeappx, ["pack", "/o", "/h", "SHA256", "/d", layout, "/p", msix]);
        if (signed) sign(msix);
        packages.push(msix);
      }
      const staging = path.join(work, `${who.kind}-bundle`);
      fs.mkdirSync(staging, { recursive: true });
      for (const msix of packages) fs.copyFileSync(msix, path.join(staging, path.basename(msix)));
      const bundle = path.join(out, `${who.filePrefix}-${quad}.msixbundle`);
      run(makeappx, ["bundle", "/o", "/bv", quad, "/d", staging, "/p", bundle]);
      if (signed) {
        sign(bundle);
        run(signtool, ["verify", "/pa", bundle]);
      }
      files.push(...packages, bundle);
      return bundle;
    };

    const bundle = packAndBundle(identity, plan.kind !== "none");
    const where = feedLocations({
      repository: options.repository ?? releaseRepository(),
      release,
      identity,
      bundleName: path.basename(bundle),
    });
    const feedFile = path.join(out, identity.feedName);
    fs.writeFileSync(
      feedFile,
      renderFeed({ identity, quad, feedUrl: where.feed, bundleUrl: where.bundle }),
    );
    files.push(feedFile);

    let storeBundle = null;
    if (storeIdentity) {
      storeBundle = packAndBundle(storeIdentity, false);
      log("Store bundle left unsigned: the Store signs it on ingestion");
    }
    return {
      quad,
      channel: release.channel,
      signed: plan.kind,
      files,
      storeBundle,
      feed: where.feed,
    };
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
}

// ── command line ────────────────────────────────────────────────────────────────────────────

const USAGE = "usage: msix.mjs build|render --version X.Y.Z[-canary.STAMP] [options]";

/** @param {string[]} argv @param {Deps} [deps] */
export function main(argv, deps = defaultDeps()) {
  const [command, ...rest] = argv;
  const { values } = parseArgs({
    args: rest,
    options: {
      version: { type: "string" },
      arch: { type: "string" },
      out: { type: "string" },
      store: { type: "boolean", default: false },
      "unpacked-x64": { type: "string" },
      "unpacked-arm64": { type: "string" },
    },
  });
  if (!values.version) fail(USAGE);
  const arches = values.arch ? values.arch.split(",").map((a) => a.trim()) : undefined;
  if (command === "render") {
    const r = parseRelease(values.version);
    const identity = identityFor(loadIdentities(), { channel: r.channel, store: values.store });
    const template = fs.readFileSync(TEMPLATE_FILE, "utf8");
    return renderManifest({
      template,
      identity,
      quad: packageVersion(r),
      arch: arches?.[0] ?? "x64",
    });
  }
  if (command !== "build") fail(USAGE);
  const summary = build(
    {
      version: values.version,
      arches,
      store: values.store,
      out: values.out ?? RELEASE_DIR,
      unpacked: {
        x64: values["unpacked-x64"] ?? path.join(RELEASE_DIR, "win-unpacked"),
        arm64: values["unpacked-arm64"] ?? path.join(RELEASE_DIR, "win-arm64-unpacked"),
      },
    },
    deps,
  );
  return JSON.stringify(summary, null, 2);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.stdout.write(`${main(process.argv.slice(2))}\n`);
  } catch (error) {
    process.stderr.write(`[msix] ${error instanceof MsixError ? error.message : error}\n`);
    process.exit(1);
  }
}
