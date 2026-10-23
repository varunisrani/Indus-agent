import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  AZURE_CREDENTIALS,
  AZURE_GROUP,
  armWindowsSigning,
  certificateTableProblems,
  createWindowsSigner,
  findSigntool,
  PFX_SECRETS,
  peCertificateTable,
  redactArgs,
  SIGNING_NAMES,
  SigningSetupError,
  selectSigner,
  signableFiles,
  signPackedWindowsApp,
  signtoolEnv,
  signWithIndus,
  stampWindowsExecutable,
  UNSIGNED_LINE,
  wasStamped,
  // @ts-expect-error -- plain JS module without types
} from "./sign-win.mjs";

type Env = Record<string, string>;
type RunCall = { command: string; args: string[]; env: Env };
type Signer = {
  kind: string;
  configured: boolean;
  signFile(file: string): Promise<string>;
  signTree(dir: string): Promise<Array<[string, string]>>;
  cleanup(): void;
};

const require = createRequire(import.meta.url);

// Values built at run time: no credential-shaped literal sits in the repository.
const fakeValue = (name: string) => ["fake", name.toLowerCase(), "value"].join("-");
const PFX_ENV: Env = {
  [PFX_SECRETS.certificate]: Buffer.from("not a real pfx").toString("base64"),
  [PFX_SECRETS.password]: fakeValue("pfx-pass"),
};
function azureEnv(): Env {
  const env: Env = {
    [AZURE_GROUP.endpoint]: "https://eus.codesigning.azure.net",
    [AZURE_GROUP.account]: "indus-signing",
    [AZURE_GROUP.profile]: "indus-public",
    [AZURE_GROUP.dlib]: "C:\\tools\\Azure.CodeSigning.Dlib.dll",
  };
  for (const name of AZURE_CREDENTIALS as string[]) env[name] = fakeValue(name);
  return env;
}

/** A minimal PE32+ image: DOS header, PE signature, COFF header, optional header, 16 directories. */
function fakePe(extra = 512): Buffer {
  const bytes = Buffer.alloc(0x40 + 24 + 112 + 16 * 8 + extra);
  bytes.write("MZ", 0, "latin1");
  bytes.writeUInt32LE(0x40, 0x3c);
  bytes.writeUInt32LE(0x00004550, 0x40);
  const optional = 0x40 + 24;
  bytes.writeUInt16LE(0x20b, optional);
  bytes.writeUInt32LE(16, optional + 108);
  return bytes;
}

/** What signtool does to the image's tail: an 8-byte aligned certificate table at the end. */
function appendSignature(file: string): void {
  const bytes = fs.readFileSync(file);
  const optional = bytes.readUInt32LE(0x3c) + 24;
  const entry = optional + 112 + 4 * 8;
  const old = { offset: bytes.readUInt32LE(entry), size: bytes.readUInt32LE(entry + 4) };
  const body = old.size > 0 ? bytes.subarray(0, old.offset) : bytes;
  const pad = (8 - (body.length % 8)) % 8;
  const table = Buffer.alloc(16, 0xab);
  const signed = Buffer.concat([body, Buffer.alloc(pad), table]);
  signed.writeUInt32LE(body.length + pad, entry);
  signed.writeUInt32LE(table.length, entry + 4);
  fs.writeFileSync(file, signed);
}

/** A fake signtool: records each call and signs the last argument the way signtool would. */
function recorder() {
  const calls: RunCall[] = [];
  const run = async (command: string, args: string[], options: { env: Env }) => {
    calls.push({ command, args, env: options.env });
    appendSignature(args[args.length - 1] as string);
    return { code: 0 };
  };
  return { calls, run };
}

let tmp: string;
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sign-win-test-"));
});
afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

function write(rel: string, bytes: Buffer): string {
  const file = path.join(tmp, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, bytes);
  return file;
}

function fakePackager() {
  return {
    appInfo: {
      productName: "Indus",
      productFilename: "Indus",
      copyright: "Copyright © 2026 Indus Agent contributors",
      shortVersion: undefined,
      buildVersion: "1.2.3",
      shortVersionWindows: undefined,
      getVersionInWeirdWindowsForm: () => "1.2.3.0",
    },
    platformSpecificBuildOptions: {},
    getIconPath: async () => path.join(tmp, "icon.ico"),
  };
}

function newSigner(env: Env, extra: Record<string, unknown> = {}) {
  const lines: string[] = [];
  const fake = recorder();
  const signer = createWindowsSigner({
    env,
    run: fake.run,
    log: (line: string) => lines.push(line),
    tmpRoot: tmp,
    signtool: "signtool.exe",
    ...extra,
  }) as Signer;
  return { signer, lines, calls: fake.calls };
}

describe("signer selection (named secrets)", () => {
  it("is none without secrets, PFX with the pair, Azure with the full set", () => {
    expect(selectSigner({})).toEqual({ kind: "none" });
    expect(selectSigner({ [PFX_SECRETS.certificate]: "  " })).toEqual({ kind: "none" });
    expect(selectSigner(PFX_ENV)).toMatchObject({ kind: "pfx" });
    expect(selectSigner(azureEnv())).toMatchObject({
      kind: "azure",
      endpoint: "https://eus.codesigning.azure.net",
      dlib: "C:\\tools\\Azure.CodeSigning.Dlib.dll",
    });
  });

  it("refuses a partial group, naming the missing variables and never a value", () => {
    const half = { [PFX_SECRETS.password]: PFX_ENV[PFX_SECRETS.password] as string };
    expect(() => selectSigner(half)).toThrow(SigningSetupError);
    expect(() => selectSigner(half)).toThrow(PFX_SECRETS.certificate);
    const azure = azureEnv();
    delete azure[AZURE_GROUP.profile];
    delete azure[AZURE_GROUP.dlib];
    let message = "";
    try {
      selectSigner({ ...PFX_ENV, ...azure });
    } catch (error) {
      message = (error as Error).message;
    }
    expect(message).toContain(AZURE_GROUP.profile);
    expect(message).toContain(AZURE_GROUP.dlib);
    for (const value of Object.values({ ...PFX_ENV, ...azure })) {
      expect(message).not.toContain(value);
    }
  });

  it("a complete Azure group wins over a PFX (the names the MSIX path reads too)", () => {
    expect(selectSigner({ ...PFX_ENV, ...azureEnv() })).toMatchObject({ kind: "azure" });
    expect(Object.values(AZURE_GROUP)).toEqual([
      "AZURE_TRUSTED_SIGNING_ENDPOINT",
      "AZURE_TRUSTED_SIGNING_ACCOUNT",
      "AZURE_TRUSTED_SIGNING_PROFILE",
      "INDUS_AZURE_SIGNING_DLIB",
    ]);
    expect(Object.values(PFX_SECRETS)).toEqual(["WIN_CERTIFICATE", "WIN_CERTIFICATE_PASSWORD"]);
  });

  it("arming keeps the values for the signer and removes them from the environment", () => {
    const env: Env = { PATH: "/bin", ...PFX_ENV, INDUS_SIGNTOOL: "D:\\st.exe" };
    expect(armWindowsSigning(env, { exists: () => true })).toBe("pfx");
    expect(env).toEqual({ PATH: "/bin" });
    const azure: Env = { ...azureEnv() };
    expect(() => armWindowsSigning(azure, { exists: () => false })).toThrow(
      /INDUS_AZURE_SIGNING_DLIB names a file that does not exist/,
    );
    expect(azure[AZURE_GROUP.endpoint]).toBeDefined(); // nothing removed on a refusal
    expect(armWindowsSigning({ PATH: "/bin" })).toBe("none");
  });
});

describe("signtool invocation", () => {
  it("signtool's child never gets a signing value except Azure's credentials", () => {
    const env: Env = { PATH: "/bin", ...PFX_ENV, ...azureEnv() };
    const child = signtoolEnv(selectSigner(env), env);
    for (const name of SIGNING_NAMES as string[]) {
      if (!(AZURE_CREDENTIALS as string[]).includes(name)) expect(child[name]).toBeUndefined();
    }
    expect(child.PATH).toBe("/bin");
    expect(signtoolEnv(selectSigner(PFX_ENV), { ...PFX_ENV, PATH: "/bin" })).toEqual({
      PATH: "/bin",
    });
  });

  it("PFX route: SHA-256, RFC 3161 timestamp, the private .pfx; the password never logged", async () => {
    const { signer, lines, calls } = newSigner(PFX_ENV);
    const exe = write("app/Indus.exe", fakePe());
    expect(await signer.signFile(exe)).toBe("signed");
    const call = calls[0] as RunCall;
    expect(call.command).toBe("signtool.exe");
    expect(call.args.slice(0, 7)).toEqual([
      "sign",
      "/fd",
      "SHA256",
      "/tr",
      "http://timestamp.digicert.com",
      "/td",
      "SHA256",
    ]);
    const pfx = call.args[call.args.indexOf("/f") + 1] as string;
    expect(path.dirname(pfx).startsWith(tmp)).toBe(true);
    expect(fs.statSync(pfx).mode & 0o077).toBe(0);
    expect(call.args.at(-1)).toBe(exe);
    expect(redactArgs(call.args)).not.toContain(PFX_ENV[PFX_SECRETS.password]);
    for (const line of lines) expect(line).not.toContain(PFX_ENV[PFX_SECRETS.password]);
    signer.cleanup();
    expect(fs.existsSync(pfx)).toBe(false);
  });

  it("Azure route: /dlib + metadata; vendor credential names only in signtool's env", async () => {
    const env = azureEnv();
    const { signer, calls, lines } = newSigner(env);
    const exe = write("app/Indus.exe", fakePe());
    await signer.signFile(exe);
    const call = calls[0] as RunCall;
    expect(call.args).toContain("/dlib");
    expect(call.args[call.args.indexOf("/tr") + 1]).toBe("http://timestamp.acs.microsoft.com");
    const metadata = JSON.parse(
      fs.readFileSync(call.args[call.args.indexOf("/dmdf") + 1] as string, "utf8"),
    );
    expect(metadata).toEqual({
      Endpoint: env[AZURE_GROUP.endpoint],
      CodeSigningAccountName: env[AZURE_GROUP.account],
      CertificateProfileName: env[AZURE_GROUP.profile],
    });
    expect(call.args[call.args.indexOf("/dlib") + 1]).toBe(env[AZURE_GROUP.dlib]);
    for (const name of AZURE_CREDENTIALS as string[]) expect(call.env[name]).toBe(env[name]);
    expect(call.env[AZURE_GROUP.endpoint]).toBeUndefined();
    expect(call.args.join(" ")).not.toContain(env.AZURE_CLIENT_SECRET);
    for (const line of lines) expect(line).not.toContain(env.AZURE_CLIENT_SECRET);
    signer.cleanup();
  });

  it("no secrets: nothing runs and one line says the build is unsigned", async () => {
    const { signer, lines, calls } = newSigner({});
    expect(signer.configured).toBe(false);
    write("app/Indus.exe", fakePe());
    write("app/ffmpeg.dll", fakePe());
    await signer.signTree(path.join(tmp, "app"));
    await signer.signFile(path.join(tmp, "app/Indus.exe"));
    expect(calls).toEqual([]);
    expect(lines).toEqual([UNSIGNED_LINE]);
  });

  it("a failing signtool stops the build with the file and exit code", async () => {
    const { signer } = newSigner(PFX_ENV, { run: async () => ({ code: 1 }) });
    const exe = write("app/Indus.exe", fakePe());
    await expect(signer.signFile(exe)).rejects.toThrow(/exited 1 signing .*Indus\.exe/);
    signer.cleanup();
  });

  it("finds the newest SDK signtool for the host arch, or an explicit path", () => {
    const kits = "C:\\Program Files (x86)\\Windows Kits\\10\\bin";
    const found = findSigntool(
      {},
      {
        arch: "x64",
        list: () => ["10.0.19041.0", "10.0.26100.0", "10.0.22621.0", "x64"],
        exists: (file: string) => !file.includes("26100"),
      },
    );
    expect(found).toBe(`${kits}\\10.0.22621.0\\x64\\signtool.exe`);
    expect(findSigntool({ INDUS_SIGNTOOL: "D:\\st.exe" })).toBe("D:\\st.exe");
    expect(findSigntool({}, { list: () => [], exists: () => false })).toBe("signtool");
  });
});

describe("every PE we ship is signed once, after the stamp (fake signer)", () => {
  function packedTree() {
    const app = path.join(tmp, "win-unpacked");
    write("win-unpacked/Indus.exe", fakePe());
    write("win-unpacked/ffmpeg.dll", fakePe());
    write("win-unpacked/libEGL.DLL", fakePe());
    write(
      "win-unpacked/resources/app.asar.unpacked/node_modules/node-pty/build/Release/pty.node",
      fakePe(),
    );
    write(
      "win-unpacked/resources/app.asar.unpacked/node_modules/node-pty/build/Release/conpty/conpty.dll",
      fakePe(),
    );
    write(
      "win-unpacked/resources/app.asar.unpacked/node_modules/node-pty/build/Release/conpty/OpenConsole.exe",
      fakePe(),
    );
    write(
      "win-unpacked/resources/app.asar.unpacked/node_modules/other/prebuild.node",
      Buffer.from("ELF"),
    );
    write("win-unpacked/resources/app.asar", Buffer.from("asar"));
    write("win-unpacked/LICENSES.chromium.html", Buffer.from("<html>"));
    return app;
  }

  it("stamps electron.exe before the rename and integrity, then signs each PE exactly once", async () => {
    const events: string[] = [];
    const packager = fakePackager();
    const app = path.join(tmp, "win-unpacked");
    write("win-unpacked/electron.exe", fakePe());
    const context = { electronPlatformName: "win32", appOutDir: app, packager };
    const stamp = { version: "1.2.3", channel: "stable", commit: "a".repeat(40) };
    const identity = await stampWindowsExecutable(context, stamp, {
      edit: async (opts: { file: string }) => events.push(`stamp ${path.basename(opts.file)}`),
      log: () => {},
    });
    expect(identity.versionStrings).toMatchObject({
      ProductName: "Indus",
      FileDescription: "Indus",
      OriginalFilename: "Indus.exe",
      CompanyName: "Indus Agent contributors",
      IndusBuildVersion: "1.2.3",
      IndusBuildChannel: "stable",
      IndusBuildCommit: "a".repeat(40),
    });
    expect(identity.fileVersion).toBe("1.2.3");
    expect(identity.productVersion).toBe("1.2.3.0");
    expect(identity.iconPath).toBe(path.join(tmp, "icon.ico"));
    expect(wasStamped(packager, app)).toBe(true);
    // electron-builder now renames the binary and embeds the ASAR integrity resource.
    fs.rmSync(path.join(app, "electron.exe"));
    events.push("rename + integrity");
    packedTree();
    events.push("fuses");

    const { signer, calls } = newSigner(PFX_ENV);
    const outcomes = await signPackedWindowsApp(context, signer);
    for (const call of calls) events.push(`sign ${path.basename(call.args.at(-1) as string)}`);
    const signed = calls.map((c) => path.relative(app, c.args.at(-1) as string)).sort();
    const pes = (signableFiles(app) as string[])
      .map((f) => path.relative(app, f))
      .filter((f) => !f.endsWith("prebuild.node"));
    expect(signed).toEqual(pes.sort());
    expect(new Set(signed).size).toBe(signed.length);
    expect(signed).toHaveLength(6);
    expect(
      Object.fromEntries(outcomes)[
        path.join(app, "resources/app.asar.unpacked/node_modules/other/prebuild.node")
      ],
    ).toBe("skipped");
    expect(events.indexOf("stamp electron.exe")).toBe(0);
    expect(events.indexOf("fuses")).toBeLessThan(events.findIndex((e) => e.startsWith("sign ")));
    for (const rel of signed) {
      expect(certificateTableProblems(fs.readFileSync(path.join(app, rel)))).toEqual([]);
    }

    // A second pass (a rerun of the hook) signs nothing again.
    await signPackedWindowsApp(context, signer);
    expect(calls).toHaveLength(6);
    signer.cleanup();
  });

  it("refuses to sign a folder whose executable was not stamped first", async () => {
    const app = packedTree();
    const { signer, calls } = newSigner(PFX_ENV);
    const context = { electronPlatformName: "win32", appOutDir: app, packager: fakePackager() };
    await expect(signPackedWindowsApp(context, signer)).rejects.toThrow(/not stamped/);
    expect(calls).toEqual([]);
  });

  it("leaves other platforms alone", async () => {
    const { signer, calls } = newSigner(PFX_ENV);
    const linux = { electronPlatformName: "linux", appOutDir: tmp, packager: fakePackager() };
    expect(await stampWindowsExecutable(linux, undefined, { edit: async () => {} })).toBeNull();
    expect(await signPackedWindowsApp(linux, signer)).toEqual([]);
    expect(calls).toEqual([]);
  });

  it("installer and uninstaller go through the same signer; a rebuilt file is signed anew", async () => {
    const { signer, calls } = newSigner(PFX_ENV);
    const uninstaller = write("release/__uninstaller-nsis-indus.exe", fakePe());
    const installer = write("release/Indus-Setup-1.2.3-x64.exe", fakePe());
    await signWithIndus({ path: uninstaller }, signer);
    await signWithIndus({ path: installer }, signer);
    await signWithIndus({ path: installer }, signer);
    expect(calls.map((c) => path.basename(c.args.at(-1) as string))).toEqual([
      "__uninstaller-nsis-indus.exe",
      "Indus-Setup-1.2.3-x64.exe",
    ]);
    // The arm64 build writes a new uninstaller at the same temporary path.
    fs.writeFileSync(uninstaller, fakePe(1024));
    await signWithIndus({ path: uninstaller }, signer);
    expect(calls).toHaveLength(3);
    signer.cleanup();
  });

  it("fails when the image changed after signing (certificate table no longer at the end)", async () => {
    const { signer } = newSigner(PFX_ENV, {
      run: async (_c: string, args: string[]) => {
        const file = args.at(-1) as string;
        appendSignature(file);
        fs.appendFileSync(file, Buffer.from("late edit"));
        return { code: 0 };
      },
    });
    const exe = write("app/Indus.exe", fakePe());
    await expect(signer.signFile(exe)).rejects.toThrow(/does not end the file/);
    signer.cleanup();
  });
});

describe("PE certificate table", () => {
  it("reads the security directory of PE32+ and PE32 images", () => {
    const pe = fakePe();
    expect(peCertificateTable(pe)).toEqual({ offset: 0, size: 0 });
    expect(certificateTableProblems(pe)).toEqual(["no certificate table (not signed)"]);
    const pe32 = fakePe();
    const optional = 0x40 + 24;
    pe32.writeUInt16LE(0x10b, optional);
    pe32.writeUInt32LE(16, optional + 92);
    pe32.writeUInt32LE(0x1f8, optional + 96 + 32);
    pe32.writeUInt32LE(8, optional + 96 + 36);
    expect(peCertificateTable(pe32)).toEqual({ offset: 0x1f8, size: 8 });
    expect(certificateTableProblems(pe32)).toEqual([
      "certificate table does not end the file (the image changed after signing)",
    ]);
  });

  it("is null for anything that is not a PE image", () => {
    expect(peCertificateTable(Buffer.from("\x7fELF......"))).toBeNull();
    const broken = fakePe();
    broken.writeUInt32LE(0, 0x40);
    expect(peCertificateTable(broken)).toBeNull();
    expect(certificateTableProblems(Buffer.alloc(4))).toEqual(["not a PE image"]);
  });
});

describe("config wiring", () => {
  it("stamps in afterExtract and signs in afterPack only after the fuses", async () => {
    const config = require("../electron-builder.config.cjs") as Record<string, unknown>;
    const afterPack = String(config.afterPack);
    expect(afterPack.indexOf("after-pack-fuses.mjs")).toBeGreaterThan(-1);
    expect(afterPack.indexOf("signPackedWindowsApp")).toBeGreaterThan(
      afterPack.indexOf("after-pack-fuses.mjs"),
    );
    // afterExtract runs after-extract.mjs, whose win32 stamper is ours (P23-15 + P23-16 merged)
    expect(String(config.afterExtract)).toContain("after-extract.mjs");
    // @ts-expect-error -- plain JS module without types
    const { STAMPERS } = await import("./after-extract.mjs");
    expect(Object.keys(STAMPERS).sort()).toEqual(["darwin", "win32"]);
    expect(String(STAMPERS.win32)).toContain("stampWindowsExecutable");
    const win = config.win as { signtoolOptions: { sign: unknown } };
    expect(String(win.signtoolOptions.sign)).toContain("signWithIndus");
  });
});
