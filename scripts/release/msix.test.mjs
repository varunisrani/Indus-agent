// Tests for scripts/release/msix.mjs (P24-22). Run: node --test scripts/release/msix.test.mjs
// (tests/unit/release/test_msix_node.py runs them inside the Python gate). No Windows tool runs
// here: `exec` is a recorder that writes the file makeappx would have written.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  build,
  checkFeed,
  checkManifest,
  compareQuads,
  fillTemplate,
  identityFor,
  loadIdentities,
  MIN_WINDOWS,
  MsixError,
  manifestValues,
  packageVersion,
  parseRelease,
  parseXml,
  releaseRepository,
  renderManifest,
  SIGNING,
  signingPlan,
  TEMPLATE_FILE,
} from "./msix.mjs";

const SCRIPT = fileURLToPath(new URL("./msix.mjs", import.meta.url));
const TEMPLATE = fs.readFileSync(TEMPLATE_FILE, "utf8");
const IDENTITIES = loadIdentities();
// Built at run time: no credential-shaped literal in the repository (AGENTS.md).
const PASSWORD = ["not", "a", "real", "password"].join("-");
const PFX_B64 = Buffer.from(["fake", "pfx", "bytes"].join("-")).toString("base64");

/** Identities with the owner's values filled in, as they will be once the owner settles them. */
function filledIdentities() {
  const copy = structuredClone(IDENTITIES);
  copy.stable.publisher = "CN=Example Signing Subject, O=Example, C=US";
  copy.canary.publisher = "CN=Example Signing Subject, O=Example, C=US";
  copy.store.identityName = "12345Example.IndusAgent";
  copy.store.publisher = "CN=00000000-1111-2222-3333-444444444444";
  copy.store.publisherDisplayName = "Example Publisher";
  return copy;
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "msix-test-"));
}

/** Two unpacked apps (x64, arm64) with the executable the manifest names. */
function unpackedApps(root) {
  const unpacked = {};
  for (const arch of ["x64", "arm64"]) {
    const dir = path.join(root, `unpacked-${arch}`);
    fs.mkdirSync(path.join(dir, "resources"), { recursive: true });
    fs.writeFileSync(path.join(dir, "Indus.exe"), `exe ${arch}`);
    fs.writeFileSync(path.join(dir, "resources", "app.asar"), "asar");
    unpacked[arch] = dir;
  }
  return unpacked;
}

/** A recorder for makeappx / signtool: writes the /p output, snapshots bundle inputs. */
function recorder({ failSigns = 0 } = {}) {
  const calls = [];
  const lines = [];
  let signFailures = failSigns;
  const exec = (file, args) => {
    const call = { tool: path.basename(file), args: [...args] };
    if (args[0] === "pack" || args[0] === "bundle") {
      const out = args[args.indexOf("/p") + 1];
      if (args[0] === "bundle") call.inputs = fs.readdirSync(args[args.indexOf("/d") + 1]).sort();
      if (args[0] === "pack") {
        const layout = args[args.indexOf("/d") + 1];
        call.manifest = fs.readFileSync(path.join(layout, "AppxManifest.xml"), "utf8");
        call.assets = fs.readdirSync(path.join(layout, "Assets")).sort();
      }
      fs.writeFileSync(out, `${args[0]} output`);
    }
    if (args[0] === "sign") {
      const secret = args[args.indexOf(args.includes("/f") ? "/f" : "/dmdf") + 1];
      call.secretMode = fs.statSync(secret).mode & 0o777;
      call.secretBody = fs.readFileSync(secret, "utf8");
      call.secretPath = secret;
      calls.push(call);
      if (signFailures > 0) {
        signFailures -= 1;
        throw new Error("timestamp server unavailable");
      }
      return;
    }
    calls.push(call);
  };
  return { calls, lines, deps: (env = {}) => ({ env, exec, log: (l) => lines.push(l) }) };
}

const summary = (calls) =>
  calls.map((c) => `${c.tool} ${c.args[0]} ${path.basename(c.args[c.args.length - 1])}`);

// ── versions ────────────────────────────────────────────────────────────────────────────────

describe("package version quads", () => {
  it("maps stable versions to X.Y.Z.0", () => {
    assert.equal(packageVersion("0.0.1"), "0.0.1.0");
    assert.equal(packageVersion("1.2.3"), "1.2.3.0");
    assert.equal(packageVersion("12.0.65535"), "12.0.65535.0");
  });

  it("maps canary stamps to seconds since 2026-01-01 split in two 16-bit halves", () => {
    assert.equal(packageVersion("1.2.3-canary.20260101T000000Z"), "1.2.0.0");
    assert.equal(packageVersion("1.2.3-canary.20260101T001640Z"), "1.2.0.1000");
    const t = Date.UTC(2026, 9, 23, 12, 34, 56) / 1000 - Date.UTC(2026, 0, 1) / 1000;
    assert.equal(
      packageVersion("1.2.3-canary.20261023T123456Z"),
      `1.2.${Math.floor(t / 65536)}.${t % 65536}`,
    );
  });

  it("orders canaries by build time and a newer minor above older canaries", () => {
    const quads = [
      "0.3.9-canary.20260301T000000Z",
      "0.3.9-canary.20260301T000001Z",
      "0.3.9-canary.20261231T235959Z",
      "0.4.0-canary.20260101T000000Z",
    ].map(packageVersion);
    for (let i = 1; i < quads.length; i += 1) assert.ok(compareQuads(quads[i - 1], quads[i]) < 0);
  });

  it("refuses malformed versions, impossible stamps and fields over 16 bits", () => {
    for (const bad of [
      "",
      "1.2",
      "v1.2.3",
      "01.2.3",
      "1.2.3-rc.1",
      "1.2.3+canary.20260101T000000Z",
      "1.2.3-canary.2026-01-01",
      "1.2.3-canary.20260230T000000Z",
      "1.2.3-canary.20260101T246000Z",
      "1.70000.0",
      "1.0.70000",
      "1.2.3-canary.20251231T235959Z",
    ]) {
      assert.throws(() => packageVersion(bad), MsixError, bad);
    }
  });

  it("parses the channel from the version", () => {
    assert.equal(parseRelease("1.0.0").channel, "stable");
    assert.equal(parseRelease("1.0.0-canary.20260505T050505Z").channel, "canary");
  });
});

// ── manifests ───────────────────────────────────────────────────────────────────────────────

describe("manifest rendering per channel", () => {
  it("renders stable and canary with their own identities and no Store values", () => {
    for (const [version, channel] of [
      ["1.0.0", "stable"],
      ["1.0.0-canary.20260505T050505Z", "canary"],
    ]) {
      for (const arch of ["x64", "arm64"]) {
        const identity = identityFor(IDENTITIES, { channel });
        const xml = renderManifest({
          template: TEMPLATE,
          identity,
          quad: packageVersion(version),
          arch,
        });
        assert.deepEqual(checkManifest(xml), []);
        const ident = parseXml(xml).children.find((c) => c.name === "Identity");
        assert.equal(ident.attrs.Name, IDENTITIES[channel].identityName);
        assert.equal(ident.attrs.ProcessorArchitecture, arch);
        assert.equal(ident.attrs.Version, packageVersion(version));
        assert.ok(!xml.includes(IDENTITIES.store.identityName));
        assert.ok(!xml.includes(IDENTITIES.store.publisher));
      }
    }
    assert.notEqual(IDENTITIES.canary.identityName, IDENTITIES.stable.identityName);
  });

  it("sets Windows 11 22H2 as the minimum", () => {
    const identity = identityFor(IDENTITIES, { channel: "stable" });
    const xml = renderManifest({ template: TEMPLATE, identity, quad: "1.0.0.0", arch: "x64" });
    const deps = parseXml(xml).children.find((c) => c.name === "Dependencies");
    assert.equal(deps.children[0].attrs.Name, "Windows.Desktop");
    assert.equal(deps.children[0].attrs.MinVersion, "10.0.22621.0");
    assert.equal(MIN_WINDOWS, "10.0.22621.0");
  });

  it("never gives a canary the Store identity", () => {
    assert.throws(
      () => identityFor(IDENTITIES, { channel: "canary", store: true }),
      /stable releases only/,
    );
    const seeded = structuredClone(IDENTITIES);
    seeded.canary.identityName = seeded.store.identityName;
    assert.throws(() => identityFor(seeded, { channel: "canary" }), /never use the Store identity/);
    const samePublisher = structuredClone(IDENTITIES);
    samePublisher.canary.publisher = samePublisher.store.publisher;
    assert.throws(() => identityFor(samePublisher, { channel: "canary" }), /Store identity/);
    const sameAsStable = structuredClone(IDENTITIES);
    sameAsStable.canary.identityName = sameAsStable.stable.identityName;
    assert.throws(() => identityFor(sameAsStable, { channel: "canary" }), /differ from the stable/);
  });

  it("escapes values and refuses unfilled or unused fields", () => {
    assert.equal(
      fillTemplate('<a b="{{x}}"/>', { x: `R&D <"x'>` }),
      '<a b="R&amp;D &lt;&quot;x&apos;&gt;"/>',
    );
    assert.throws(() => fillTemplate("{{x}} {{y}}", { x: "1" }), /\{\{y\}\} has no value/);
    assert.throws(() => fillTemplate("{{x}}", { x: "1", y: "2" }), /never uses: y/);
    assert.throws(() => fillTemplate("{{ x }}", {}), /malformed/);
    const values = manifestValues(identityFor(IDENTITIES, { channel: "stable" }), "1.0.0.0", "x64");
    assert.doesNotThrow(() => fillTemplate(TEMPLATE, values));
  });

  it("refuses an unsupported architecture", () => {
    const identity = identityFor(IDENTITIES, { channel: "stable" });
    assert.throws(() =>
      renderManifest({ template: TEMPLATE, identity, quad: "1.0.0.0", arch: "ia64" }),
    );
  });
});

describe("schema validation of the rendered manifest", () => {
  const good = renderManifest({
    template: TEMPLATE,
    identity: identityFor(IDENTITIES, { channel: "stable" }),
    quad: "1.0.0.0",
    arch: "x64",
  });
  /** @type {Array<[string, (x: string) => string, RegExp]>} */
  const seeded = [
    [
      "MinVersion below 22H2",
      (x) => x.replace('MinVersion="10.0.22621.0"', 'MinVersion="10.0.19041.0"'),
      /below Windows 11 22H2/,
    ],
    [
      "MaxVersionTested below MinVersion",
      (x) => x.replace(/MaxVersionTested="[^"]+"/, 'MaxVersionTested="10.0.22000.0"'),
      /MaxVersionTested/,
    ],
    [
      "no runFullTrust",
      (x) => x.replace('<rescap:Capability Name="runFullTrust" />', ""),
      /runFullTrust/,
    ],
    [
      "virtualization off without the capability",
      (x) => x.replace('<rescap:Capability Name="unvirtualizedResources" />', ""),
      /unvirtualizedResources/,
    ],
    ["unfilled field", (x) => x.replace('Version="1.0.0.0"', 'Version="{{version}}"'), /unfilled/],
    [
      "bad publisher",
      (x) => x.replace(/Publisher="[^"]+"/, 'Publisher="Indus"'),
      /distinguished name/,
    ],
    [
      "bad package name",
      (x) => x.replace(/Name="IndusAgent\.Desktop"/, 'Name="In"'),
      /valid package name/,
    ],
    [
      "reserved package name",
      (x) => x.replace(/Name="IndusAgent\.Desktop"/, 'Name="COM1.app"'),
      /valid package name/,
    ],
    [
      "version field over 16 bits",
      (x) => x.replace('Version="1.0.0.0"', 'Version="1.0.70000.0"'),
      /not a quad/,
    ],
    [
      "capability after a device capability",
      (x) =>
        x.replace(
          '<DeviceCapability Name="microphone" />',
          '<DeviceCapability Name="microphone" /><Capability Name="internetClient" />',
        ),
      /after a DeviceCapability/,
    ],
    [
      "children out of order",
      (x) =>
        x.replace(
          /(<Resources>[\s\S]*?<\/Resources>)\s*(<Applications>[\s\S]*?<\/Applications>)/,
          "$2$1",
        ),
      /out of order/,
    ],
    [
      "undeclared prefix",
      (x) =>
        x.replace(
          'IgnorableNamespaces="uap rescap desktop6"',
          'IgnorableNamespaces="uap rescap desktop6 uap10"',
        ),
      /undeclared prefix uap10/,
    ],
    [
      "reserved protocol",
      (x) => x.replace('<uap:Protocol Name="indus">', '<uap:Protocol Name="https">'),
      /protocol/,
    ],
    [
      "absolute executable",
      (x) => x.replace('Executable="Indus.exe"', 'Executable="C:\\Indus.exe"'),
      /relative \.exe/,
    ],
    ["missing tile", (x) => x.replace(/Square44x44Logo="[^"]+"/, ""), /Square44x44Logo/],
    ["not well-formed", (x) => x.replace("</Properties>", "</Propertie>"), /well-formed/],
  ];
  it("accepts the real rendered manifest", () => assert.deepEqual(checkManifest(good), []));
  for (const [label, mutate, expected] of seeded) {
    it(`catches: ${label}`, () => {
      const broken = mutate(good);
      assert.notEqual(broken, good, `the mutation for "${label}" did not apply`);
      const problems = checkManifest(broken);
      assert.ok(
        problems.some((p) => expected.test(p)),
        `${label}: ${problems.join(" | ")}`,
      );
    });
  }
});

describe("XML reader", () => {
  it("decodes entities and keeps attributes", () => {
    const root = parseXml(
      '<?xml version="1.0"?><!-- c --><a x="1 &amp; 2"><b>&lt;&#65;&#x42;</b></a>',
    );
    assert.equal(root.attrs.x, "1 & 2");
    assert.equal(root.children[0].text, "<AB");
  });
  it("refuses what our documents never hold", () => {
    for (const bad of [
      "<!DOCTYPE a><a/>",
      '<a x="1" x="2"/>',
      "<a><b></a>",
      "<a/><b/>",
      "<a>&nbsp;</a>",
      "<a>& b</a>",
      "<a><![CDATA[x]]></a>",
      "<a><!-- x -- y --></a>",
    ]) {
      assert.throws(() => parseXml(bad), MsixError, bad);
    }
  });
});

// ── signing selection ───────────────────────────────────────────────────────────────────────

describe("signing selection by secret names", () => {
  const azureEnv = () =>
    Object.fromEntries(SIGNING.azure.map((n) => [n, `${n.toLowerCase()}-value`]));
  it("is unsigned with no secrets", () => assert.deepEqual(signingPlan({}), { kind: "none" }));
  it("uses a PFX when both PFX names are set", () => {
    const plan = signingPlan({ WIN_CERTIFICATE: PFX_B64, WIN_CERTIFICATE_PASSWORD: PASSWORD });
    assert.equal(plan.kind, "pfx");
    assert.match(plan.timestampUrl, /^http/);
  });
  it("prefers Azure Trusted Signing when its group is complete", () => {
    const env = { ...azureEnv(), WIN_CERTIFICATE: PFX_B64, WIN_CERTIFICATE_PASSWORD: PASSWORD };
    assert.equal(signingPlan(env).kind, "azure");
    assert.equal(signingPlan(env).timestampUrl, "http://timestamp.acs.microsoft.com");
  });
  it("refuses a partly configured group, naming what is missing", () => {
    assert.throws(
      () => signingPlan({ WIN_CERTIFICATE: PFX_B64 }),
      /WIN_CERTIFICATE_PASSWORD not set/,
    );
    const env = azureEnv();
    delete env.AZURE_TRUSTED_SIGNING_PROFILE;
    assert.throws(() => signingPlan(env), /AZURE_TRUSTED_SIGNING_PROFILE not set/);
  });
  it("lets the timestamp server be overridden", () => {
    const env = {
      WIN_CERTIFICATE: PFX_B64,
      WIN_CERTIFICATE_PASSWORD: PASSWORD,
      INDUS_SIGN_TIMESTAMP_URL: "http://ts.example",
    };
    assert.equal(signingPlan(env).timestampUrl, "http://ts.example");
  });
});

// ── build ───────────────────────────────────────────────────────────────────────────────────

describe("build", () => {
  const repository = "example-owner/example-repo";

  it("packs per arch, bundles only this run's packages, writes the feed, unsigned with one line", () => {
    const root = tempDir();
    try {
      const out = path.join(root, "out");
      fs.mkdirSync(out);
      fs.writeFileSync(path.join(out, "Stray-1.0.0.0-x64.msix"), "left by another run");
      const rec = recorder();
      const result = build(
        { version: "1.4.2", unpacked: unpackedApps(root), out, repository },
        rec.deps({}),
      );
      assert.deepEqual(summary(rec.calls), [
        "makeappx pack Indus-1.4.2.0-x64.msix",
        "makeappx pack Indus-1.4.2.0-arm64.msix",
        "makeappx bundle Indus-1.4.2.0.msixbundle",
      ]);
      assert.deepEqual(rec.calls[2].inputs, ["Indus-1.4.2.0-arm64.msix", "Indus-1.4.2.0-x64.msix"]);
      assert.ok(rec.calls[2].args.includes("/bv") && rec.calls[2].args.includes("1.4.2.0"));
      assert.deepEqual(rec.calls[0].assets, [
        "Square150x150Logo.png",
        "Square44x44Logo.png",
        "StoreLogo.png",
      ]);
      assert.deepEqual(checkManifest(rec.calls[0].manifest), []);
      assert.equal(rec.lines.filter((l) => /unsigned/.test(l)).length, 1);
      assert.equal(result.signed, "none");
      assert.equal(result.storeBundle, null);
      const feed = fs.readFileSync(path.join(out, "Indus.appinstaller"), "utf8");
      assert.deepEqual(checkFeed(feed), []);
      const main = parseXml(feed).children.find((c) => c.name === "MainBundle");
      assert.equal(main.attrs.Name, IDENTITIES.stable.identityName);
      assert.equal(main.attrs.Version, "1.4.2.0");
      assert.equal(
        main.attrs.Uri,
        `https://github.com/${repository}/releases/download/v1.4.2/Indus-1.4.2.0.msixbundle`,
      );
      assert.equal(
        parseXml(feed).attrs.Uri,
        `https://github.com/${repository}/releases/latest/download/Indus.appinstaller`,
      );
      assert.ok(!fs.existsSync(path.join(out, ".msix-work")), "work folder removed");
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("signs each package, then the bundle, then verifies; the password never reaches the log", () => {
    const root = tempDir();
    try {
      const rec = recorder();
      const env = { WIN_CERTIFICATE: PFX_B64, WIN_CERTIFICATE_PASSWORD: PASSWORD };
      const result = build(
        {
          version: "1.4.2",
          unpacked: unpackedApps(root),
          out: path.join(root, "out"),
          repository,
          identities: filledIdentities(),
        },
        rec.deps(env),
      );
      assert.deepEqual(summary(rec.calls), [
        "makeappx pack Indus-1.4.2.0-x64.msix",
        "signtool sign Indus-1.4.2.0-x64.msix",
        "makeappx pack Indus-1.4.2.0-arm64.msix",
        "signtool sign Indus-1.4.2.0-arm64.msix",
        "makeappx bundle Indus-1.4.2.0.msixbundle",
        "signtool sign Indus-1.4.2.0.msixbundle",
        "signtool verify Indus-1.4.2.0.msixbundle",
      ]);
      const sign = rec.calls[1];
      assert.ok(sign.args.includes("/tr") && sign.args.includes("/fd"));
      assert.equal(sign.secretMode, 0o600);
      assert.equal(sign.secretBody, Buffer.from(PFX_B64, "base64").toString("utf8"));
      assert.ok(!fs.existsSync(sign.secretPath), "the PFX file is removed afterwards");
      assert.equal(result.signed, "pfx");
      assert.ok(rec.lines.length > 0 && rec.lines.every((l) => !l.includes(PASSWORD)));
      assert.ok(rec.lines.some((l) => l.includes("***")));
      assert.ok(!rec.lines.some((l) => /unsigned/.test(l)));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("signs with Azure Trusted Signing through the dlib and a metadata file", () => {
    const root = tempDir();
    try {
      const rec = recorder();
      const env = Object.fromEntries(SIGNING.azure.map((n) => [n, `${n.toLowerCase()}-value`]));
      build(
        {
          version: "1.4.2",
          arches: ["x64"],
          unpacked: unpackedApps(root),
          out: path.join(root, "out"),
          repository,
          identities: filledIdentities(),
        },
        rec.deps(env),
      );
      const sign = rec.calls.find((c) => c.tool === "signtool" && c.args[0] === "sign");
      assert.ok(
        sign.args.includes("/dlib") && sign.args.includes("indus_azure_signing_dlib-value"),
      );
      assert.deepEqual(JSON.parse(sign.secretBody), {
        Endpoint: "azure_trusted_signing_endpoint-value",
        CodeSigningAccountName: "azure_trusted_signing_account-value",
        CertificateProfileName: "azure_trusted_signing_profile-value",
      });
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("retries a failed sign up to three times, then fails and still cleans up", () => {
    const root = tempDir();
    try {
      const env = { WIN_CERTIFICATE: PFX_B64, WIN_CERTIFICATE_PASSWORD: PASSWORD };
      const ok = recorder({ failSigns: 2 });
      build(
        {
          version: "1.0.0",
          arches: ["x64"],
          unpacked: unpackedApps(root),
          out: path.join(root, "a"),
          repository,
          identities: filledIdentities(),
        },
        ok.deps(env),
      );
      assert.equal(ok.calls.filter((c) => c.args[0] === "sign").length, 4); // 3 for the msix, 1 bundle
      const bad = recorder({ failSigns: 99 });
      assert.throws(
        () =>
          build(
            {
              version: "1.0.0",
              arches: ["x64"],
              unpacked: unpackedApps(root),
              out: path.join(root, "b"),
              repository,
              identities: filledIdentities(),
            },
            bad.deps(env),
          ),
        /timestamp server/,
      );
      assert.equal(bad.calls.filter((c) => c.args[0] === "sign").length, 3);
      assert.ok(!fs.existsSync(path.join(root, "b", ".msix-work")));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("refuses to sign with the placeholder publisher", () => {
    const root = tempDir();
    try {
      const env = { WIN_CERTIFICATE: PFX_B64, WIN_CERTIFICATE_PASSWORD: PASSWORD };
      const rec = recorder();
      assert.throws(
        () =>
          build(
            {
              version: "1.0.0",
              unpacked: unpackedApps(root),
              out: path.join(root, "out"),
              repository,
            },
            rec.deps(env),
          ),
        /publisher is a placeholder/,
      );
      assert.equal(rec.calls.length, 0);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("builds the canary with its own identity, files and feed location", () => {
    const root = tempDir();
    try {
      const rec = recorder();
      const out = path.join(root, "out");
      const version = "0.5.0-canary.20261023T101010Z";
      const quad = packageVersion(version);
      build({ version, unpacked: unpackedApps(root), out, repository }, rec.deps({}));
      assert.equal(path.basename(rec.calls[2].args.at(-1)), `Indus-Canary-${quad}.msixbundle`);
      assert.match(rec.calls[0].manifest, /Name="IndusAgent\.Desktop\.Canary"/);
      const feed = parseXml(fs.readFileSync(path.join(out, "Indus-Canary.appinstaller"), "utf8"));
      assert.equal(
        feed.attrs.Uri,
        `https://github.com/${repository}/releases/download/channels/Indus-Canary.appinstaller`,
      );
      assert.match(
        feed.children[0].attrs.Uri,
        new RegExp(`/download/v${version.replaceAll(".", "\\.")}/`),
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("refuses a Store bundle for anything but a stable release", () => {
    const root = tempDir();
    try {
      const rec = recorder();
      assert.throws(
        () =>
          build(
            {
              version: "1.0.0-canary.20261023T101010Z",
              store: true,
              unpacked: unpackedApps(root),
              out: path.join(root, "out"),
              repository,
              identities: filledIdentities(),
            },
            rec.deps({}),
          ),
        /stable releases only/,
      );
      assert.equal(rec.calls.length, 0);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("refuses a Store bundle before 1.0.0", () => {
    const root = tempDir();
    try {
      assert.throws(
        () =>
          build(
            {
              version: "0.9.0",
              store: true,
              unpacked: unpackedApps(root),
              out: path.join(root, "out"),
              repository,
              identities: filledIdentities(),
            },
            recorder().deps({}),
          ),
        /1\.0\.0 or later/,
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("refuses a Store bundle while the Store identity is a placeholder", () => {
    const root = tempDir();
    try {
      assert.throws(
        () =>
          build(
            {
              version: "1.0.0",
              store: true,
              unpacked: unpackedApps(root),
              out: path.join(root, "out"),
              repository,
            },
            recorder().deps({}),
          ),
        /Store identity identityName is still a placeholder/,
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("builds the Store bundle unsigned with the Store identity, even when signing is configured", () => {
    const root = tempDir();
    try {
      const rec = recorder();
      const env = { WIN_CERTIFICATE: PFX_B64, WIN_CERTIFICATE_PASSWORD: PASSWORD };
      const identities = filledIdentities();
      const result = build(
        {
          version: "1.0.0",
          store: true,
          unpacked: unpackedApps(root),
          out: path.join(root, "out"),
          repository,
          identities,
        },
        rec.deps(env),
      );
      const storeCalls = rec.calls.filter((c) => c.args.at(-1).includes("Indus-Store-"));
      assert.deepEqual(summary(storeCalls), [
        "makeappx pack Indus-Store-1.0.0.0-x64.msix",
        "makeappx pack Indus-Store-1.0.0.0-arm64.msix",
        "makeappx bundle Indus-Store-1.0.0.0.msixbundle",
      ]);
      assert.deepEqual(storeCalls[2].inputs, [
        "Indus-Store-1.0.0.0-arm64.msix",
        "Indus-Store-1.0.0.0-x64.msix",
      ]);
      for (const call of storeCalls.slice(0, 2)) {
        assert.match(call.manifest, new RegExp(`Name="${identities.store.identityName}"`));
        assert.deepEqual(checkManifest(call.manifest), []);
      }
      assert.equal(path.basename(result.storeBundle), "Indus-Store-1.0.0.0.msixbundle");
      assert.ok(rec.lines.some((l) => /Store bundle left unsigned/.test(l)));
      // The sideload bundle is still signed.
      assert.ok(
        rec.calls.some(
          (c) => c.args[0] === "sign" && c.args.at(-1).endsWith("Indus-1.0.0.0.msixbundle"),
        ),
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("refuses a missing unpacked app or one without the executable", () => {
    const root = tempDir();
    try {
      const unpacked = unpackedApps(root);
      fs.rmSync(path.join(unpacked.arm64, "Indus.exe"));
      assert.throws(
        () =>
          build(
            { version: "1.0.0", unpacked, out: path.join(root, "o"), repository },
            recorder().deps({}),
          ),
        /has no Indus\.exe/,
      );
      assert.throws(
        () =>
          build(
            {
              version: "1.0.0",
              unpacked: { x64: path.join(root, "nope") },
              arches: ["x64"],
              out: path.join(root, "o"),
              repository,
            },
            recorder().deps({}),
          ),
        /not found/,
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("feed and release base", () => {
  it("reads the release base from versions.py", () => {
    const text = fs.readFileSync(
      new URL("../../src/indus/foundation/versions.py", import.meta.url),
      "utf8",
    );
    const expected = /^RELEASE_REPOSITORY = "([^"]+)"/m.exec(text)[1];
    assert.equal(releaseRepository(), expected);
  });
  it("catches a broken feed", () => {
    assert.ok(checkFeed("<AppInstaller/>").length > 0);
    assert.ok(checkFeed("<Other/>")[0].includes("expected <AppInstaller>"));
  });
});

// ── command line (fake makeappx on PATH; POSIX shells only) ─────────────────────────────────

describe("command line", {
  skip: process.platform === "win32" && "fake tools are POSIX scripts",
}, () => {
  it("builds with a fake makeappx and prints a JSON summary", () => {
    const root = tempDir();
    try {
      const fake = path.join(root, "makeappx");
      // Writes the file named after /p, like the real tool.
      fs.writeFileSync(
        fake,
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = "/p" ]; then : > "$2"; fi; shift; done\n',
        { mode: 0o755 },
      );
      const unpacked = unpackedApps(root);
      const out = path.join(root, "out");
      const env = { PATH: process.env.PATH, INDUS_MAKEAPPX: fake };
      const run = spawnSync(
        process.execPath,
        [
          SCRIPT,
          "build",
          "--version",
          "1.0.0",
          "--out",
          out,
          "--unpacked-x64",
          unpacked.x64,
          "--unpacked-arm64",
          unpacked.arm64,
        ],
        { env, encoding: "utf8" },
      );
      assert.equal(run.status, 0, run.stderr);
      const result = JSON.parse(run.stdout);
      assert.equal(result.quad, "1.0.0.0");
      assert.equal(result.signed, "none");
      assert.equal((run.stderr.match(/unsigned/g) ?? []).length, 1);
      assert.ok(fs.existsSync(path.join(out, "Indus-1.0.0.0.msixbundle")));
      assert.ok(fs.existsSync(path.join(out, "Indus.appinstaller")));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("renders a valid manifest and exits 1 on a Store request for a canary", () => {
    const render = spawnSync(
      process.execPath,
      [SCRIPT, "render", "--version", "1.0.0", "--arch", "arm64"],
      { encoding: "utf8" },
    );
    assert.equal(render.status, 0, render.stderr);
    assert.deepEqual(checkManifest(render.stdout), []);
    const refused = spawnSync(
      process.execPath,
      [SCRIPT, "render", "--version", "1.0.0-canary.20261023T101010Z", "--store"],
      { encoding: "utf8" },
    );
    assert.equal(refused.status, 1);
    assert.match(refused.stderr, /stable releases only/);
  });
});
