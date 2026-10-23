// Windows identity stamp and Authenticode signing for the desktop build (P23-16, D-16.70).
//
// Order inside one electron-builder run, per architecture:
//   1. afterExtract   stampWindowsExecutable: the version resource and icon go into the pristine
//                     `electron.exe`, before electron-builder renames it and embeds the ASAR
//                     integrity resource (electron-builder's own late edit is off:
//                     `signAndEditExecutable: false`)
//   2. afterPack      after the fuses are flipped: signPackedWindowsApp signs every .exe, .dll and
//                     .node in the packed folder, once
//   3. NSIS target    electron-builder hands the uninstaller and the installer to the same signer
//                     (`win.signtoolOptions.sign` → signWithIndus)
//
// The signer is picked from named secrets (the same names the MSIX path reads, P24-22) and never
// prints a value:
//   Azure Trusted  AZURE_TRUSTED_SIGNING_ENDPOINT, _ACCOUNT, _PROFILE + INDUS_AZURE_SIGNING_DLIB
//   Signing        (the client dlib; it authenticates with the vendor's AZURE_TENANT_ID,
//                  AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)                   → signtool /dlib
//   PFX            WIN_CERTIFICATE (base64 .pfx) + WIN_CERTIFICATE_PASSWORD  → signtool /f
//   neither        one line, the build stays unsigned
// A complete Azure group wins over a PFX; a partial group stops the build naming what is missing.
// Only the Windows release entry (dist-win.mjs) arms the signer: armWindowsSigning keeps these
// values for this process and deletes them from the environment, so electron-builder, its tools
// and the packaging scripts never see them; dist-unsigned.mjs alone never signs. signtool's child
// gets the vendor credential names only.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);

export const SIGNABLE_EXTENSIONS = Object.freeze([".exe", ".dll", ".node"]);
export const PFX_SECRETS = Object.freeze({
  certificate: "WIN_CERTIFICATE",
  password: "WIN_CERTIFICATE_PASSWORD",
});
export const AZURE_GROUP = Object.freeze({
  endpoint: "AZURE_TRUSTED_SIGNING_ENDPOINT",
  account: "AZURE_TRUSTED_SIGNING_ACCOUNT",
  profile: "AZURE_TRUSTED_SIGNING_PROFILE",
  dlib: "INDUS_AZURE_SIGNING_DLIB",
});
/** The vendor's credential names the Trusted Signing dlib reads (passed through to signtool). */
export const AZURE_CREDENTIALS = Object.freeze([
  "AZURE_TENANT_ID",
  "AZURE_CLIENT_ID",
  "AZURE_CLIENT_SECRET",
]);
export const SIGNTOOL_PATH = "INDUS_SIGNTOOL";
export const TIMESTAMP_OVERRIDE = "INDUS_WIN_TIMESTAMP_URL";
/** RFC 3161 timestamp services: the CA's for a PFX (override INDUS_WIN_TIMESTAMP_URL), Azure's own. */
export const PFX_TIMESTAMP_URL = "http://timestamp.digicert.com";
export const AZURE_TIMESTAMP_URL = "http://timestamp.acs.microsoft.com";
/** The Electron binary's name inside the extracted distribution, before electron-builder renames it. */
export const PRISTINE_EXECUTABLE = "electron.exe";

export class SigningSetupError extends Error {}

function present(env, name) {
  return typeof env[name] === "string" && env[name].trim() !== "";
}

/** Every variable the signer may read (what captureSigningEnv keeps). */
export const SIGNING_NAMES = Object.freeze([
  ...Object.values(PFX_SECRETS),
  ...Object.values(AZURE_GROUP),
  ...AZURE_CREDENTIALS,
  SIGNTOOL_PATH,
  TIMESTAMP_OVERRIDE,
]);

/**
 * Pick the signer from the environment. Returns `{ kind: "none" }`, `{ kind: "azure", … }` or
 * `{ kind: "pfx", … }`; throws SigningSetupError (names only) on a partial group.
 */
export function selectSigner(env = process.env) {
  const azureNames = Object.values(AZURE_GROUP);
  const azureGiven = azureNames.filter((name) => present(env, name));
  if (azureGiven.length > 0) {
    const missing = azureNames.filter((name) => !present(env, name));
    if (missing.length > 0) {
      throw new SigningSetupError(`Azure Trusted Signing needs ${missing.join(", ")}`);
    }
    const value = (key) => env[AZURE_GROUP[key]].trim();
    const credentials = {};
    for (const name of AZURE_CREDENTIALS) if (present(env, name)) credentials[name] = env[name];
    return {
      kind: "azure",
      endpoint: value("endpoint"),
      account: value("account"),
      profile: value("profile"),
      dlib: value("dlib"),
      credentials,
      timestampUrl: AZURE_TIMESTAMP_URL,
    };
  }
  const pfxNames = Object.values(PFX_SECRETS);
  if (pfxNames.some((name) => present(env, name))) {
    const missing = pfxNames.filter((name) => !present(env, name));
    if (missing.length > 0) throw new SigningSetupError(`PFX signing needs ${missing.join(", ")}`);
    return {
      kind: "pfx",
      certificate: env[PFX_SECRETS.certificate].trim(),
      password: env[PFX_SECRETS.password],
      timestampUrl: env[TIMESTAMP_OVERRIDE]?.trim() || PFX_TIMESTAMP_URL,
    };
  }
  return { kind: "none" };
}

/** The line printed once when no signer is configured. */
export const UNSIGNED_LINE =
  `[sign-win] no Windows signing secrets (${AZURE_GROUP.endpoint} … or ` +
  `${PFX_SECRETS.certificate}): the build stays unsigned`;

/**
 * signtool's arguments for one file. `material` holds the private files written for the run
 * (`pfxFile` or `metadataFile`).
 */
export function signtoolArgs(signer, file, material) {
  const common = ["sign", "/fd", "SHA256", "/tr", signer.timestampUrl, "/td", "SHA256"];
  if (signer.kind === "pfx") {
    return [...common, "/f", material.pfxFile, "/p", signer.password, file];
  }
  if (signer.kind === "azure") {
    return [...common, "/dlib", signer.dlib, "/dmdf", material.metadataFile, file];
  }
  throw new SigningSetupError(`no signtool arguments for signer ${signer.kind}`);
}

/** What signtool's child gets: the env without any signing value, plus Azure's credentials. */
export function signtoolEnv(signer, env = process.env) {
  const out = {};
  for (const [key, value] of Object.entries(env))
    if (!SIGNING_NAMES.includes(key)) out[key] = value;
  return signer.kind === "azure" ? { ...out, ...signer.credentials } : out;
}

/** A log-safe view of signtool's arguments: the password position is masked. */
export function redactArgs(args) {
  return args.map((arg, i) => (i > 0 && args[i - 1] === "/p" ? "***" : arg));
}

/**
 * signtool.exe: INDUS_SIGNTOOL, else the newest Windows 10/11 SDK copy for the host arch,
 * else `signtool` on PATH.
 */
export function findSigntool(env = process.env, deps = {}) {
  if (present(env, SIGNTOOL_PATH)) return env[SIGNTOOL_PATH].trim();
  const exists = deps.exists ?? fs.existsSync;
  const list = deps.list ?? ((dir) => fs.readdirSync(dir));
  const hostArch = deps.arch ?? os.arch();
  const kits = path.win32.join(
    env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
    "Windows Kits",
    "10",
    "bin",
  );
  let versions = [];
  try {
    versions = list(kits).filter((name) => /^10\.\d+\.\d+\.\d+$/.test(name));
  } catch {
    versions = [];
  }
  const numeric = (v) => v.split(".").map(Number);
  versions.sort((a, b) => {
    const [x, y] = [numeric(a), numeric(b)];
    for (let i = 0; i < 4; i += 1) if (x[i] !== y[i]) return (y[i] ?? 0) - (x[i] ?? 0);
    return 0;
  });
  const folder = hostArch === "arm64" ? "arm64" : "x64";
  for (const version of versions) {
    const candidate = path.win32.join(kits, version, folder, "signtool.exe");
    if (exists(candidate)) return candidate;
  }
  return "signtool";
}

// -- PE checks -----------------------------------------------------------------------------------

/**
 * The Authenticode certificate table of a PE image: `{ offset, size }` (zero size when unsigned),
 * or null when `bytes` is not a PE image.
 */
export function peCertificateTable(bytes) {
  if (bytes.length < 0x40 || bytes[0] !== 0x4d || bytes[1] !== 0x5a) return null;
  const pe = bytes.readUInt32LE(0x3c);
  if (pe + 24 > bytes.length || bytes.readUInt32LE(pe) !== 0x00004550) return null;
  const optional = pe + 24;
  if (optional + 2 > bytes.length) return null;
  const magic = bytes.readUInt16LE(optional);
  // PE32 (0x10b) and PE32+ (0x20b) differ in where the directory count and table start.
  let countAt;
  let directoriesAt;
  if (magic === 0x10b) [countAt, directoriesAt] = [92, 96];
  else if (magic === 0x20b) [countAt, directoriesAt] = [108, 112];
  else return null;
  if (optional + directoriesAt + 5 * 8 > bytes.length) return null;
  if (bytes.readUInt32LE(optional + countAt) < 5) return { offset: 0, size: 0 };
  const entry = optional + directoriesAt + 4 * 8; // IMAGE_DIRECTORY_ENTRY_SECURITY
  return { offset: bytes.readUInt32LE(entry), size: bytes.readUInt32LE(entry + 4) };
}

/** Problems with a signed image's certificate table (empty when it is consistent). */
export function certificateTableProblems(bytes) {
  const table = peCertificateTable(bytes);
  if (table === null) return ["not a PE image"];
  if (table.size === 0) return ["no certificate table (not signed)"];
  const problems = [];
  if (table.offset % 8 !== 0) problems.push("certificate table is not 8-byte aligned");
  if (table.offset + table.size !== bytes.length) {
    problems.push("certificate table does not end the file (the image changed after signing)");
  }
  return problems;
}

// -- the signer ----------------------------------------------------------------------------------

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function isSignableName(file) {
  const lower = file.toLowerCase();
  return SIGNABLE_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/** Every file under `dir` with a signable extension, sorted (stable order, stable logs). */
export function signableFiles(dir) {
  const out = [];
  const walk = (current) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile() && isSignableName(entry.name)) out.push(full);
    }
  };
  walk(dir);
  return out.sort();
}

function runChild(command, args, options) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      env: options.env,
      stdio: ["ignore", "inherit", "inherit"],
      windowsHide: true,
    });
    child.on("error", (error) => resolve({ code: -1, error }));
    child.on("close", (code) => resolve({ code: code ?? -1 }));
  });
}

/**
 * One signer for a build process. `deps.run(command, args, { env })` runs signtool (a recorder in
 * tests); `deps.log` prints one line; `deps.tmpRoot` holds the private run folder.
 */
export function createWindowsSigner(deps = {}) {
  const env = deps.env ?? process.env;
  const run = deps.run ?? runChild;
  const log = deps.log ?? ((line) => process.stdout.write(`${line}\n`));
  const signer = selectSigner(env);
  /** path → digest of the file as we left it after signing */
  const ledger = new Map();
  let material = null;
  let toolPath = null;
  let saidUnsigned = false;

  function prepare() {
    if (material !== null) return material;
    const dir = fs.mkdtempSync(path.join(deps.tmpRoot ?? os.tmpdir(), "indus-sign-"));
    fs.chmodSync(dir, 0o700);
    material = { dir };
    if (signer.kind === "pfx") {
      material.pfxFile = path.join(dir, "certificate.pfx");
      fs.writeFileSync(material.pfxFile, Buffer.from(signer.certificate, "base64"), {
        mode: 0o600,
      });
    } else if (signer.kind === "azure") {
      material.metadataFile = path.join(dir, "metadata.json");
      const metadata = {
        Endpoint: signer.endpoint,
        CodeSigningAccountName: signer.account,
        CertificateProfileName: signer.profile,
      };
      fs.writeFileSync(material.metadataFile, `${JSON.stringify(metadata)}\n`, { mode: 0o600 });
    }
    toolPath = deps.signtool ?? findSigntool(env);
    return material;
  }

  function cleanup() {
    if (material !== null) fs.rmSync(material.dir, { recursive: true, force: true });
    material = null;
  }

  /** Sign one file unless it is already ours; returns "signed", "kept" or "skipped". */
  async function signFile(file) {
    const full = path.resolve(file);
    if (signer.kind === "none") {
      if (!saidUnsigned) log(UNSIGNED_LINE);
      saidUnsigned = true;
      return "skipped";
    }
    const before = fs.readFileSync(full);
    if (peCertificateTable(before) === null) {
      log(`[sign-win] not a PE image, left unsigned: ${full}`);
      return "skipped";
    }
    if (ledger.get(full) === sha256(before)) return "kept";
    const own = prepare();
    const args = signtoolArgs(signer, full, own);
    const result = await run(toolPath, args, { env: signtoolEnv(signer, env) });
    if (result.code !== 0) {
      throw new Error(`[sign-win] signtool exited ${result.code} signing ${full}`);
    }
    const after = fs.readFileSync(full);
    const problems = certificateTableProblems(after);
    if (problems.length > 0) throw new Error(`[sign-win] ${full}: ${problems.join("; ")}`);
    ledger.set(full, sha256(after));
    log(`[sign-win] signed (${signer.kind}) ${full}`);
    return "signed";
  }

  return {
    kind: signer.kind,
    configured: signer.kind !== "none",
    ledger,
    signFile,
    cleanup,
    async signTree(dir) {
      const outcomes = [];
      for (const file of signableFiles(dir)) outcomes.push([file, await signFile(file)]);
      return outcomes;
    },
  };
}

let shared = null;
let armed = {};

/**
 * Arm this process's signer from `env` (dist-win.mjs, before anything is built): keep the signing
 * values, delete them from `env`, return the signer kind. A partial group, or an Azure dlib path
 * that does not exist, fails here. Without arming, the signer is "none" whatever the environment.
 */
export function armWindowsSigning(env = process.env, deps = {}) {
  const exists = deps.exists ?? fs.existsSync;
  const kept = {};
  for (const name of SIGNING_NAMES) if (present(env, name)) kept[name] = env[name];
  const signer = selectSigner(kept);
  if (signer.kind === "azure" && !exists(signer.dlib)) {
    throw new SigningSetupError(`${AZURE_GROUP.dlib} names a file that does not exist`);
  }
  for (const name of SIGNING_NAMES) delete env[name];
  armed = kept;
  shared = null;
  return signer.kind;
}

/** The process-wide signer (built on first use, its private files removed at exit). */
export function sharedSigner() {
  if (shared === null) {
    const env = { ...process.env };
    for (const name of SIGNING_NAMES) delete env[name];
    shared = createWindowsSigner({ env: { ...env, ...armed } });
    process.once("exit", () => shared?.cleanup());
  }
  return shared;
}

// -- the hooks -----------------------------------------------------------------------------------

const stamped = new WeakMap();

function markStamped(packager, appOutDir) {
  const dirs = stamped.get(packager) ?? new Set();
  dirs.add(path.resolve(appOutDir));
  stamped.set(packager, dirs);
}

/** Was this packed folder's executable stamped by the afterExtract hook? */
export function wasStamped(packager, appOutDir) {
  return stamped.get(packager)?.has(path.resolve(appOutDir)) ?? false;
}

/** electron-builder's own resource editor (resedit), loaded from its package. */
async function loadResourceEditor() {
  const builderRequire = createRequire(require.resolve("electron-builder/package.json"));
  const root = path.dirname(builderRequire.resolve("app-builder-lib/package.json"));
  const mod = await import(pathToFileURL(path.join(root, "out", "util", "resEdit.js")).href);
  return mod.editWindowsResources;
}

/**
 * The version resource we stamp: product, description, copyright, file name, versions, icon, and
 * with a build stamp (P20-38) its version, channel and commit, as the macOS stamp does.
 */
export async function executableIdentity(packager, stamp) {
  const info = packager.appInfo;
  const versionStrings = {
    FileDescription: info.productName,
    ProductName: info.productName,
    LegalCopyright: info.copyright,
    CompanyName: "Indus Agent contributors",
    InternalName: info.productFilename,
    OriginalFilename: `${info.productFilename}.exe`,
    ...(stamp
      ? {
          IndusBuildVersion: stamp.version,
          IndusBuildChannel: stamp.channel,
          IndusBuildCommit: stamp.commit,
        }
      : {}),
  };
  return {
    versionStrings,
    fileVersion: info.shortVersion || info.buildVersion,
    productVersion: info.shortVersionWindows || info.getVersionInWeirdWindowsForm(),
    requestedExecutionLevel: packager.platformSpecificBuildOptions?.requestedExecutionLevel,
    iconPath: (await packager.getIconPath()) ?? undefined,
  };
}

/**
 * afterExtract (Windows only): stamp the pristine `electron.exe` before the rename and the ASAR
 * integrity resource. Shaped as an after-extract.mjs stamper `(context, stamp)`; `deps.edit`
 * replaces the resource editor in tests.
 */
export async function stampWindowsExecutable(context, stamp = undefined, deps = {}) {
  if (context.electronPlatformName !== "win32") return null;
  const exe = path.join(context.appOutDir, PRISTINE_EXECUTABLE);
  if (!fs.existsSync(exe)) {
    throw new Error(`[sign-win] ${PRISTINE_EXECUTABLE} is missing from ${context.appOutDir}`);
  }
  const edit = deps.edit ?? (await loadResourceEditor());
  const identity = await executableIdentity(context.packager, stamp);
  await edit({ file: exe, ...identity });
  markStamped(context.packager, context.appOutDir);
  (deps.log ?? ((line) => process.stdout.write(`${line}\n`)))(
    `[sign-win] identity stamped into ${PRISTINE_EXECUTABLE} (${identity.versionStrings.ProductName} ${identity.fileVersion})`,
  );
  return identity;
}

/**
 * afterPack (Windows only), after the fuses: refuse a folder whose executable was not stamped,
 * then sign every PE in it once.
 */
export async function signPackedWindowsApp(context, signer = sharedSigner()) {
  if (context.electronPlatformName !== "win32") return [];
  if (!wasStamped(context.packager, context.appOutDir)) {
    throw new Error(
      "[sign-win] the executable identity was not stamped in afterExtract; signing now would " +
        "freeze an unstamped binary",
    );
  }
  return signer.signTree(context.appOutDir);
}

/** `win.signtoolOptions.sign`: electron-builder's hand-off for the uninstaller and installer. */
export async function signWithIndus(configuration, signer = sharedSigner()) {
  await signer.signFile(configuration.path);
}
