"""P24-21: the static APT repository for the Termux channel (layout, signing seam, exit 3), and
the validation that runs before anything is signed (W50-d-3, exit 4)."""

from __future__ import annotations

import gzip
import hashlib
import io
import lzma
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.termux import repo
from scripts.termux.repo import GpgSigner, RepoError, build_repository, read_control

WHEN = datetime(2026, 10, 23, 0, 0, 0, tzinfo=UTC)
EPOCH = str(int(WHEN.timestamp()))
STAMP = "20261021T064100Z"
CONTROL = (
    "Package: indus-agent\n"
    "Version: {version}\n"
    "Architecture: {arch}\n"
    "Maintainer: Indus Agent maintainers <maintainers@indus.invalid>\n"
    "Installed-Size: 12\n"
    "Description: Indus Agent for Termux\n"
    " A test package.\n"
)


# -- building test packages ------------------------------------------------------------------------


def _tar(files: Mapping[str, bytes], *, executable: frozenset[str] = frozenset()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(body), 0
            info.mode = 0o755 if name in executable else 0o644
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


PREFIX = "./data/data/com.termux/files/usr"
LAUNCHER = f"{PREFIX}/bin/indus"
HOME_DIR = f"{PREFIX}/lib/indus-agent"
STAMP_FILE = f"{HOME_DIR}/app/.install_method"


def termux_layout(launcher: bytes = b"#!/bin/sh\n") -> dict[str, bytes]:
    """The entries `check_payload` looks for, as `build.sh` lays them out."""
    return {
        LAUNCHER: launcher,
        f"{HOME_DIR}/venv/bin/python": b"\x7fELF",
        f"{HOME_DIR}/app/src/indus/__init__.py": b'__version__ = "1.2.3"\n',
        STAMP_FILE: b"apt\n",
    }


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, body in members:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(body):<10}".encode() + b"`\n"
        out += header + body + (b"\n" if len(body) % 2 else b"")
    return bytes(out)


def make_deb(
    *,
    version: str = "1.2.3-1",
    arch: str = "aarch64",
    control: str | None = None,
    member: str = "control.tar.xz",
    payload: bytes = b"#!/bin/sh\n",
    extra: Mapping[str, bytes] | None = None,
    layout: Mapping[str, bytes] | None = None,
    executable: frozenset[str] = frozenset({LAUNCHER}),
) -> bytes:
    text = control if control is not None else CONTROL.format(version=version, arch=arch)
    inner = _tar({"./control": text.encode(), **(extra or {})})
    packed = {
        "control.tar.xz": lambda b: lzma.compress(b),
        "control.tar.gz": lambda b: gzip.compress(b, mtime=0),
        "control.tar": lambda b: b,
    }.get(member, lambda b: b)(inner)
    files = termux_layout(payload) if layout is None else layout
    data = lzma.compress(_tar(files, executable=executable))
    return _ar([("debian-binary", b"2.0\n"), (member, packed), ("data.tar.xz", data)])


def write_debs(folder: Path, *debs: bytes) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, body in enumerate(debs):
        path = folder / f"input-{index}.deb"
        path.write_bytes(body)
        paths.append(path)
    return paths


class FakeSigner:
    """Records what it signed; writes recognisable stand-ins for the signature files."""

    def __init__(self) -> None:
        self.signed: list[bytes] = []

    def sign(self, release: Path, *, inline: Path, detached: Path) -> None:
        body = release.read_bytes()
        self.signed.append(body)
        inline.write_bytes(b"CLEARSIGNED\n" + body)
        detached.write_bytes(b"DETACHED " + hashlib.sha256(body).hexdigest().encode() + b"\n")

    def public_key(self) -> bytes:
        return b"PUBLIC KEY STAND-IN\n"


def listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- layout ----------------------------------------------------------------------------------------


def test_golden_repository_listing(tmp_path: Path) -> None:
    deb = make_deb()
    debs = write_debs(tmp_path / "in", deb)
    signer = FakeSigner()
    result = build_repository(debs, tmp_path / "repo", "v1.2.3", signer=signer, when=WHEN)
    assert result.suite == "indus-stable" and result.signed
    assert result.added == ("indus-agent_1.2.3-1",)
    out = tmp_path / "repo"
    assert listing(out) == [
        "dists/indus-stable/InRelease",
        "dists/indus-stable/Release",
        "dists/indus-stable/Release.gpg",
        "dists/indus-stable/main/binary-aarch64/Packages",
        "dists/indus-stable/main/binary-aarch64/Packages.gz",
        "key.asc",
        "pool/main/i/indus-agent/indus-agent_1.2.3-1_aarch64.deb",
    ]
    binary = out / "dists/indus-stable/main/binary-aarch64"
    packages = (binary / "Packages").read_bytes()
    assert packages.decode() == (
        CONTROL.format(version="1.2.3-1", arch="aarch64")
        + "Filename: pool/main/i/indus-agent/indus-agent_1.2.3-1_aarch64.deb\n"
        + f"Size: {len(deb)}\n"
        + f"SHA256: {sha(deb)}\n"
    )
    packed = (binary / "Packages.gz").read_bytes()
    assert gzip.decompress(packed) == packages
    release = (out / "dists/indus-stable/Release").read_text()
    assert release == (
        "Origin: Indus Agent\n"
        "Label: indus-agent\n"
        "Suite: indus-stable\n"
        "Codename: indus-stable\n"
        "Date: Fri, 23 Oct 2026 00:00:00 UTC\n"
        "Architectures: aarch64\n"
        "Components: main\n"
        "Description: Indus Agent for Termux (stable channel)\n"
        "SHA256:\n"
        f" {sha(packages)} {len(packages):>16} main/binary-aarch64/Packages\n"
        f" {sha(packed)} {len(packed):>16} main/binary-aarch64/Packages.gz\n"
    )
    assert "\n\n" not in release  # one paragraph: apt ignores sums after a blank line
    assert signer.signed == [release.encode()]
    assert (out / "pool/main/i/indus-agent/indus-agent_1.2.3-1_aarch64.deb").read_bytes() == deb
    assert (out / "key.asc").read_bytes() == b"PUBLIC KEY STAND-IN\n"


def test_output_is_reproducible(tmp_path: Path) -> None:
    debs = write_debs(tmp_path / "in", make_deb())
    for name in ("a", "b"):
        build_repository(debs, tmp_path / name, "v1.2.3", signer=None, when=WHEN)
    for rel in listing(tmp_path / "a"):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes(), rel


def test_canary_goes_to_its_own_suite(tmp_path: Path) -> None:
    version = f"1.2.4~canary.{STAMP}-1"
    debs = write_debs(tmp_path / "in", make_deb(version=version))
    result = build_repository(
        debs, tmp_path / "repo", f"v1.2.4-canary.{STAMP}", signer=None, when=WHEN
    )
    assert result.suite == "indus-canary" and not result.signed
    names = listing(tmp_path / "repo")
    assert f"pool/main/i/indus-agent/indus-agent_{version}_aarch64.deb" in names
    assert "dists/indus-canary/Release" in names
    assert not any(n.startswith("dists/indus-stable") for n in names)


def test_stable_and_canary_accumulate_in_order(tmp_path: Path) -> None:
    out = tmp_path / "repo"
    for tag, version in (("v1.2.10", "1.2.10-1"), ("v1.2.9", "1.2.9-1")):
        debs = write_debs(tmp_path / tag, make_deb(version=version))
        build_repository(debs, out, tag, signer=None, when=WHEN)
    text = (out / "dists/indus-stable/main/binary-aarch64/Packages").read_text()
    stanzas = text.split("\n\n")
    assert len(stanzas) == 2  # separated by exactly one blank line
    assert "Version: 1.2.9-1" in stanzas[0] and "Version: 1.2.10-1" in stanzas[1]
    late, early = f"v1.3.0-canary.{STAMP}", "v1.3.0-canary.20261020T000000Z"
    for tag in (late, early):
        version = repo.deb_version(tag)
        debs = write_debs(tmp_path / tag, make_deb(version=version))
        build_repository(debs, out, tag, signer=None, when=WHEN)
    canary = (out / "dists/indus-canary/main/binary-aarch64/Packages").read_text()
    assert canary.index("20261020T000000Z") < canary.index(STAMP)
    assert "1.2.10-1" in (out / "dists/indus-stable/main/binary-aarch64/Packages").read_text()


# -- immutability ----------------------------------------------------------------------------------


def test_a_published_version_is_immutable(tmp_path: Path) -> None:
    out = tmp_path / "repo"
    first = write_debs(tmp_path / "one", make_deb())
    build_repository(first, out, "v1.2.3", signer=None, when=WHEN)
    before = {rel: (out / rel).read_bytes() for rel in listing(out)}
    again = build_repository(first, out, "v1.2.3", signer=None, when=WHEN)
    assert again.added == ()  # identical bytes: an idempotent rerun
    assert {rel: (out / rel).read_bytes() for rel in listing(out)} == before
    other = write_debs(tmp_path / "two", make_deb(payload=b"#!/bin/sh\necho changed\n"))
    with pytest.raises(RepoError, match="published versions never change"):
        build_repository(other, out, "v1.2.3", signer=None, when=WHEN)
    assert {rel: (out / rel).read_bytes() for rel in listing(out)} == before


# -- signing and exit codes ------------------------------------------------------------------------


def test_unsigned_release_exits_3_and_drops_stale_signatures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "repo"
    write_debs(tmp_path / "in", make_deb())
    build_repository(
        sorted((tmp_path / "in").glob("*.deb")), out, "v1.2.3", signer=FakeSigner(), when=WHEN
    )
    assert (out / "dists/indus-stable/InRelease").is_file()
    args = ["build", "--debs", str(tmp_path / "in"), "--out", str(out), "--tag", "v1.2.3"]
    assert repo.main([*args, "--date", EPOCH]) == repo.EXIT_UNSIGNED == 3
    dists = out / "dists/indus-stable"
    assert (dists / "Release").is_file()
    assert not (dists / "InRelease").exists() and not (dists / "Release.gpg").exists()
    assert "UNSIGNED" in capsys.readouterr().err


def test_unsigned_when_gpg_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_debs(tmp_path / "in", make_deb())
    key = tmp_path / "key.asc"
    key.write_text("not used\n")
    monkeypatch.setattr(repo.shutil, "which", lambda _name: None)
    args = ["build", "--debs", str(tmp_path / "in"), "--out", str(tmp_path / "repo")]
    assert repo.main([*args, "--tag", "v1.2.3", "--key-file", str(key)]) == 3


@pytest.mark.parametrize(
    "extra",
    [
        ["--key-file", "/nonexistent/key.asc"],
        ["--date", "yesterday"],
        ["--tag", "v1.2.3+canary.20261021T064100Z"],
    ],
)
def test_cli_refusals_exit_2(tmp_path: Path, extra: list[str]) -> None:
    write_debs(tmp_path / "in", make_deb())
    args = ["build", "--debs", str(tmp_path / "in"), "--out", str(tmp_path / "repo")]
    if "--tag" not in extra:
        args += ["--tag", "v1.2.3"]
    assert repo.main([*args, *extra]) == repo.EXIT_REFUSED
    assert not (tmp_path / "repo" / "dists").exists()
    assert repo.main(["build"]) == repo.EXIT_REFUSED
    assert repo.main(["nonsense"]) == repo.EXIT_REFUSED


def _fake_gpg(
    listing_text: str, status: str
) -> tuple[list[tuple[list[str], bytes | None]], repo.Runner]:
    calls: list[tuple[list[str], bytes | None]] = []

    def run(
        argv: list[str], stdin: bytes | None, env: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]:
        calls.append((argv, stdin))
        assert env["GNUPGHOME"] == env["HOME"] and "--homedir" in argv
        if "--list-secret-keys" in argv:
            return 0, listing_text.encode(), b""
        if "--verify" in argv:
            return 0, status.encode(), b""
        if "--export" in argv:
            return 0, b"PUBLIC\n", b""
        if "--output" in argv:
            Path(argv[argv.index("--output") + 1]).write_bytes(b"signature\n")
        return 0, b"", b""

    return calls, run


FPR = "AB" * 20
ONE_KEY = (
    f"sec:u:255:22:X:1::::::scESC:\nfpr:::::::::{FPR}:\n"
    f"ssb:u:255:18:Y:1:::::e:\nfpr:::::::::{'CD' * 20}:\n"
)


def test_gpg_signer_keeps_the_passphrase_out_of_argv(tmp_path: Path) -> None:
    key = tmp_path / "key.asc"
    key.write_bytes(b"armoured key stand-in\n")
    calls, run = _fake_gpg(ONE_KEY, f"[GNUPG:] VALIDSIG {FPR} 2026-10-23 0 4 0 22 10 01 {FPR}\n")
    unlock = b"open-" + b"sesame"
    with GpgSigner.open(key, unlock=unlock, gpg="gpg", runner=run) as signer:
        assert signer.fingerprint == FPR
        release = tmp_path / "Release"
        release.write_text("Origin: x\n")
        signer.sign(release, inline=tmp_path / "InRelease", detached=tmp_path / "Release.gpg")
        assert signer.public_key() == b"PUBLIC\n"
        home = signer.home
    assert not home.exists()  # the throwaway home is gone
    assert calls[0][1] == b"armoured key stand-in\n"  # the key goes in on stdin
    signing = [(argv, stdin) for argv, stdin in calls if "--output" in argv]
    assert len(signing) == 2
    for argv, stdin in signing:
        assert stdin == unlock and "--passphrase-fd" in argv and "--local-user" in argv
    for argv, _stdin in calls:
        assert all(unlock.decode() not in part for part in argv)


def test_gpg_signer_refuses_several_keys_and_bad_signatures(tmp_path: Path) -> None:
    key = tmp_path / "key.asc"
    key.write_bytes(b"k\n")
    two = ONE_KEY + f"sec:u:255:22:Z:1::::::scESC:\nfpr:::::::::{'EF' * 20}:\n"
    _calls, run = _fake_gpg(two, "")
    with pytest.raises(RepoError, match="holds 2 secret keys"), GpgSigner.open(key, runner=run):
        pass
    _calls, run = _fake_gpg("", "")
    with pytest.raises(RepoError, match="no secret key"), GpgSigner.open(key, runner=run):
        pass
    other = "EF" * 20
    _calls, run = _fake_gpg(
        ONE_KEY, f"[GNUPG:] VALIDSIG {other} 2026-10-23 0 4 0 22 10 01 {other}\n"
    )
    release = tmp_path / "Release"
    release.write_text("Origin: x\n")
    with GpgSigner.open(key, runner=run) as signer, pytest.raises(RepoError, match="no valid"):
        signer.sign(release, inline=tmp_path / "InRelease", detached=tmp_path / "Release.gpg")


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is not installed")
@pytest.mark.parametrize("route", ["one-call", "build-validate-sign"])
def test_real_gpg_signs_and_verifies(tmp_path: Path, route: str) -> None:
    """End to end with a throwaway key made here (no key material is stored in the repo), both as
    one `build --key-file` call and as the three steps `build.sh` runs (W50-d-3)."""
    home = tmp_path / "keyring"
    home.mkdir(mode=0o700)
    env = {"GNUPGHOME": str(home), "HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}

    def gpg(*args: str) -> bytes:
        return subprocess.run(
            ["gpg", "--batch", "--homedir", str(home), *args],
            check=True,
            capture_output=True,
            env=env,
            timeout=120,
        ).stdout

    try:
        gpg("--passphrase", "", "--quick-gen-key", "Repo Test <t@example.invalid>", "ed25519")
        (tmp_path / "secret.asc").write_bytes(gpg("--armor", "--export-secret-keys"))
        write_debs(tmp_path / "in", make_deb())
        out = tmp_path / "repo"
        args = ["build", "--debs", str(tmp_path / "in"), "--out", str(out), "--tag", "v1.2.3"]
        key = ["--key-file", str(tmp_path / "secret.asc")]
        if route == "one-call":
            assert repo.main([*args, *key, "--date", EPOCH]) == repo.EXIT_OK
        else:
            assert repo.main([*args, "--date", EPOCH]) == repo.EXIT_UNSIGNED
            suite = ["--out", str(out), "--tag", "v1.2.3"]
            assert repo.main(["validate", *suite]) == repo.EXIT_OK
            assert repo.main(["sign", *suite, *key]) == repo.EXIT_OK
        dists = out / "dists/indus-stable"
        gpg("--verify", str(dists / "InRelease"))
        gpg("--verify", str(dists / "Release.gpg"), str(dists / "Release"))
        assert b"BEGIN PGP PUBLIC KEY BLOCK" in (out / "key.asc").read_bytes()
    finally:
        if shutil.which("gpgconf"):
            subprocess.run(
                ["gpgconf", "--homedir", str(home), "--kill", "all"],
                check=False,
                env=env,
                timeout=30,
            )


# -- refused packages ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("deb", "message"),
    [
        (make_deb(arch="arm"), "aarch64 only"),
        (make_deb(arch="all"), "aarch64 only"),
        (make_deb(version="1.2.4-1"), "is not 1.2.3-1"),
        (make_deb(control="Package: indus-agent\nArchitecture: aarch64\n"), "no Version"),
        (make_deb(control=CONTROL.format(version="1.2.3-1", arch="aarch64") * 2), "twice"),
        (
            make_deb(control=CONTROL.format(version="1.2.3-1", arch="aarch64") + "\nX: y\n"),
            "one paragraph",
        ),
        (
            make_deb(control="Package: ../evil\nVersion: 1.2.3-1\nArchitecture: aarch64\n"),
            "not a Debian package name",
        ),
        (
            make_deb(control="Package: indus-agent\nVersion: 1.2.3-1/x\nArchitecture: aarch64\n"),
            "not a Debian version",
        ),
        (make_deb(member="control.tar.zst"), "unsupported control member"),
        (b"PK\x03\x04 not a deb", "no ar header"),
        (make_deb(extra={"./pad": bytes(repo.CONTROL_LIMIT + 1)}), "larger than"),
    ],
    ids=[
        "arch-arm",
        "arch-all",
        "version-mismatch",
        "missing-field",
        "duplicate-fields",
        "two-paragraphs",
        "unsafe-name",
        "unsafe-version",
        "zstd",
        "not-ar",
        "oversized-control",
    ],
)
def test_unsafe_or_wrong_packages_are_refused(tmp_path: Path, deb: bytes, message: str) -> None:
    debs = write_debs(tmp_path / "in", deb)
    with pytest.raises(RepoError, match=message):
        build_repository(debs, tmp_path / "repo", "v1.2.3", signer=None, when=WHEN)
    assert not (tmp_path / "repo").exists()


def test_other_inputs_are_refused(tmp_path: Path) -> None:
    with pytest.raises(RepoError, match="no .deb"):
        build_repository([], tmp_path / "repo", "v1.2.3", signer=None, when=WHEN)
    twice = write_debs(tmp_path / "in", make_deb(), make_deb(payload=b"x"))
    with pytest.raises(RepoError, match="given twice"):
        build_repository(twice, tmp_path / "repo", "v1.2.3", signer=None, when=WHEN)


@pytest.mark.parametrize("member", ["control.tar.gz", "control.tar"])
def test_other_control_compressions_are_read(member: str) -> None:
    control = read_control(make_deb(member=member))
    assert control.get("Package") == "indus-agent" and control.get("version") == "1.2.3-1"
    assert control.get("Description") == "Indus Agent for Termux\n A test package."


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb is not installed")
def test_a_dpkg_deb_package_is_read(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "DEBIAN").mkdir(parents=True)
    (tree / "DEBIAN/control").write_text(CONTROL.format(version="1.2.3-1", arch="aarch64"))
    (tree / "data/data/com.termux/files/usr/bin").mkdir(parents=True)
    (tree / "data/data/com.termux/files/usr/bin/indus").write_text("#!/bin/sh\n")
    deb = tmp_path / "built.deb"
    subprocess.run(
        ["dpkg-deb", "--build", "-Zxz", "--root-owner-group", str(tree), str(deb)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    control = read_control(deb.read_bytes())
    assert control.get("Architecture") == "aarch64"


# -- validation before signing (W50-d-3, W50-d-4) ------------------------------------------------

POOL_FILE = "pool/main/i/indus-agent/indus-agent_1.2.3-1_aarch64.deb"
SUITE_DIR = "dists/indus-stable"
INDEX = f"{SUITE_DIR}/main/binary-aarch64/Packages"


def assembled(tmp_path: Path, *debs: bytes, tag: str = "v1.2.3") -> Path:
    """An unsigned tree, as `build.sh`'s repo stage leaves it."""
    out = tmp_path / "repo"
    paths = write_debs(tmp_path / f"in-{tag}", *(debs or (make_deb(),)))
    build_repository(paths, out, tag, signer=None, when=WHEN)
    return out


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_an_assembled_tree_validates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = assembled(tmp_path)
    canary = "v1.2.4-canary.20261021T064100Z"
    assembled(tmp_path, make_deb(version="1.2.4~canary.20261021T064100Z-1"), tag=canary)
    assert repo.validate_repository(out, "v1.2.3") == []
    assert repo.validate_repository(out, canary) == []
    assert repo.main(["validate", "--out", str(out), "--tag", "v1.2.3"]) == repo.EXIT_OK
    assert "indus-stable: valid; indus-agent 1.2.3-1 is laid out for Termux" in (
        capsys.readouterr().err
    )
    assert repo.main(["validate", "--out", str(out), "--tag", "v1.2.3+canary.x"]) == 2


def _drop_release(out: Path) -> None:
    (out / SUITE_DIR / "Release").unlink()


def _grow_index(out: Path) -> None:
    with (out / INDEX).open("a", encoding="utf-8") as index:
        index.write("\n")


def _third_index(out: Path) -> None:
    with (out / SUITE_DIR / "Release").open("a", encoding="utf-8") as release:
        release.write(f" {'0' * 64}                1 main/binary-aarch64/Extra\n")


def _climbing_name(out: Path) -> None:
    _edit(out / SUITE_DIR / "Release", " main/binary-aarch64/Packages.gz", " ../Packages.gz")


def _wrong_suite(out: Path) -> None:
    _edit(out / SUITE_DIR / "Release", "Suite: indus-stable", "Suite: indus-canary")


def _garbage_gz(out: Path) -> None:
    (out / f"{INDEX}.gz").write_bytes(b"not gzip at all")


def _drop_pool_file(out: Path) -> None:
    (out / POOL_FILE).unlink()


def _swap_pool_file(out: Path) -> None:
    (out / POOL_FILE).write_bytes(make_deb(payload=b"#!/bin/sh\necho other\n"))


def _other_control(out: Path) -> None:
    (out / POOL_FILE).write_bytes(make_deb(version="1.2.5-1"))


def _escaping_filename(out: Path) -> None:
    _edit(out / INDEX, f"Filename: {POOL_FILE}", "Filename: ../../evil.deb")


def _no_digest(out: Path) -> None:
    text = (out / INDEX).read_text(encoding="utf-8")
    kept = [line for line in text.splitlines(keepends=True) if not line.startswith("SHA256:")]
    (out / INDEX).write_text("".join(kept), encoding="utf-8")


def _listed_twice(out: Path) -> None:
    text = (out / INDEX).read_text(encoding="utf-8")
    (out / INDEX).write_text(text + "\n" + text, encoding="utf-8")


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (_drop_release, "Release is missing (was the repository assembled?)"),
        (_grow_index, "main/binary-aarch64/Packages does not match its Release entry"),
        (_grow_index, "Packages.gz does not hold the same bytes as Packages"),
        (_third_index, "Release lists ["),
        (_climbing_name, "malformed SHA256 line"),
        (_wrong_suite, "Release Suite is 'indus-canary', not 'indus-stable'"),
        (_garbage_gz, "Packages.gz is not gzip"),
        (_drop_pool_file, f"indus-agent 1.2.3-1: {POOL_FILE} is missing from the pool"),
        (_swap_pool_file, "does not match the stanza's SHA256"),
        (_other_control, "has Version 1.2.5-1"),
        (_escaping_filename, "Filename ../../evil.deb is not the pool path"),
        (_no_digest, "the stanza has no SHA256"),
        (_listed_twice, "indus-agent 1.2.3-1 is listed twice"),
    ],
)
def test_a_damaged_tree_is_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path], None],
    message: str,
) -> None:
    out = assembled(tmp_path)
    damage(out)
    problems = repo.validate_repository(out, "v1.2.3")
    assert [p for p in problems if message in p], problems
    suite = ["--out", str(out), "--tag", "v1.2.3"]
    assert repo.main(["validate", *suite]) == repo.EXIT_INVALID == 4
    assert message in capsys.readouterr().err
    assert repo.main(["sign", *suite]) == repo.EXIT_INVALID  # checked before the key is looked at
    assert not (out / SUITE_DIR / "InRelease").exists() and not (out / "key.asc").exists()


def test_the_tags_version_must_be_listed(tmp_path: Path) -> None:
    out = assembled(tmp_path)
    assert repo.validate_repository(out, "v1.2.4") == [
        "indus-agent 1.2.4-1 (the version of v1.2.4) is not listed in indus-stable"
    ]
    assert repo.validate_repository(tmp_path / "nothing", "v1.2.3") == [
        "dists/indus-stable/Release is missing (was the repository assembled?)"
    ]


def _without(name: str) -> dict[str, bytes]:
    files = termux_layout()
    del files[name]
    return files


@pytest.mark.parametrize(
    ("deb", "message"),
    [
        (make_deb(layout=_without(STAMP_FILE)), "no install stamp /data/data/com.termux"),
        (
            make_deb(layout={**termux_layout(), STAMP_FILE: b"git\n"}),
            "the install stamp says 'git', not 'apt'",
        ),
        (make_deb(executable=frozenset()), "/usr/bin/indus is not executable"),
        (
            make_deb(layout=_without(LAUNCHER)),
            "no launcher /data/data/com.termux/files/usr/bin/indus",
        ),
        (make_deb(layout=_without(f"{HOME_DIR}/venv/bin/python")), "no environment python"),
        (
            make_deb(layout=_without(f"{HOME_DIR}/app/src/indus/__init__.py")),
            "no application package",
        ),
        (
            make_deb(layout={**termux_layout(), "./etc/profile": b"x", "./../up": b"y"}),
            "2 payload entries outside /data/data/com.termux/files/usr: './etc/profile', './../up'",
        ),
        (
            _ar([("debian-binary", b"2.0\n"), ("control.tar", _tar({"./control": b"x"}))]),
            "no data member in the package",
        ),
        (
            _ar([("debian-binary", b"2.0\n"), ("data.tar.zst", b"zstd")]),
            "unsupported data member 'data.tar.zst'",
        ),
        (
            _ar([("debian-binary", b"2.0\n"), ("data.tar.xz", b"not xz")]),
            "unreadable data member",
        ),
    ],
)
def test_the_payload_layout_is_checked(deb: bytes, message: str) -> None:
    problems = repo.check_payload(deb)
    assert [p for p in problems if message in p], problems


def test_a_good_payload_has_no_problems() -> None:
    assert repo.check_payload(make_deb()) == []
    stamp_with_bom = {**termux_layout(), STAMP_FILE: "\ufeffAPT\r\n".encode()}
    assert repo.check_payload(make_deb(layout=stamp_with_bom)) == []
    plain = _ar(
        [
            ("debian-binary", b"2.0\n"),
            ("data.tar", _tar(termux_layout(), executable=frozenset({LAUNCHER}))),
        ]
    )
    assert repo.check_payload(plain) == []


def test_nothing_is_signed_when_the_package_is_not_laid_out_for_termux(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package without its `apt` stamp would leave `indus update` unable to send the user to
    `pkg`: the one-call build refuses to sign it, and so does `sign`."""
    unstamped = make_deb(layout=_without(STAMP_FILE))
    signer = FakeSigner()
    debs = write_debs(tmp_path / "in", unstamped)
    with pytest.raises(repo.InvalidRepository, match="no install stamp") as caught:
        build_repository(debs, tmp_path / "repo", "v1.2.3", signer=signer, when=WHEN)
    assert caught.value.suite == "indus-stable" and len(caught.value.problems) == 1
    assert signer.signed == []
    out = tmp_path / "repo"
    assert (out / SUITE_DIR / "Release").is_file()  # written, but never signed
    assert not (out / SUITE_DIR / "InRelease").exists() and not (out / "key.asc").exists()
    with pytest.raises(repo.InvalidRepository):
        repo.sign_repository(out, "v1.2.3", signer)
    assert signer.signed == []
    key = tmp_path / "key.asc"
    key.write_text("not used\n")

    @contextmanager
    def fake_open(*_args: object, **_kwargs: object) -> Iterator[FakeSigner]:
        yield signer

    monkeypatch.setattr(repo.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(repo.GpgSigner, "open", fake_open)
    args = ["build", "--debs", str(tmp_path / "in"), "--out", str(out), "--tag", "v1.2.3"]
    assert repo.main([*args, "--key-file", str(key)]) == repo.EXIT_INVALID
    assert signer.signed == []
    err = capsys.readouterr().err
    assert "indus-stable: indus-agent 1.2.3-1: no install stamp" in err
    assert "1 problem(s); nothing was signed" in err


def test_sign_validates_then_signs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = assembled(tmp_path)
    suite = ["--out", str(out), "--tag", "v1.2.3"]
    assert repo.main(["sign", *suite]) == repo.EXIT_UNSIGNED
    assert "valid but left UNSIGNED (no --key-file given)" in capsys.readouterr().err
    assert repo.main(["sign", *suite, "--key-file", str(tmp_path / "missing.asc")]) == 2
    signer = FakeSigner()
    assert repo.sign_repository(out, "v1.2.3", signer) == "indus-stable"
    release = (out / SUITE_DIR / "Release").read_bytes()
    assert signer.signed == [release]
    assert (out / SUITE_DIR / "InRelease").read_bytes() == b"CLEARSIGNED\n" + release
    assert (out / "key.asc").read_bytes() == b"PUBLIC KEY STAND-IN\n"
