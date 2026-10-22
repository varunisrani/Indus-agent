"""P25-04 dependency audit: lock parsing, OSV lookups (faked), the expiring allow-list, provenance,
the offline snapshot guard and the pinned-download probe. No test touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tooling.checks import deps_audit
from tooling.checks.common import Context
from tooling.checks.deps_audit import (
    OSV_BATCH,
    OSV_VULN,
    Advisory,
    AuditUnavailable,
    Package,
    Policy,
    cvss3_score,
    evaluate,
    osv_severity,
    parse_pnpm_audit,
    query_osv,
    read_pnpm_lock,
    read_policy,
    read_uv_lock,
    resolve_pins,
    write_snapshot,
)

TODAY = date(2026, 10, 21)
HIGH = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N"  # 9.1 critical
RECORDS: dict[str, dict[str, Any]] = {
    "GHSA-demo-0001": {
        "id": "GHSA-demo-0001",
        "aliases": ["PYSEC-demo-1", "CVE-0000-0001"],
        "summary": "template sandbox escape",
        "database_specific": {"severity": "HIGH"},
        "affected": [
            {
                "package": {"name": "jinja2", "ecosystem": "PyPI"},
                "ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.10.1"}]}],
            }
        ],
    },
    "PYSEC-demo-1": {"id": "PYSEC-demo-1", "aliases": ["GHSA-demo-0001"], "summary": ""},
    "GHSA-demo-0002": {
        "id": "GHSA-demo-0002",
        "summary": "prototype pollution",
        "database_specific": {"severity": "MODERATE"},
    },
}
HITS = {
    ("PyPI", "jinja2", "2.4.1"): ["GHSA-demo-0001", "PYSEC-demo-1"],
    ("npm", "lodash", "4.17.0"): ["GHSA-demo-0002"],
}


def fake_osv(
    hits: dict[tuple[str, str, str], list[str]] = HITS,
    records: dict[str, dict[str, Any]] = RECORDS,
    calls: list[str] | None = None,
) -> Callable[[str, bytes | None], Any]:
    def fetch(url: str, body: bytes | None) -> Any:
        if calls is not None:
            calls.append(url)
        if url == OSV_BATCH:
            assert body is not None
            queries = json.loads(body)["queries"]
            results: list[dict[str, Any]] = []
            for query in queries:
                key = (query["package"]["ecosystem"], query["package"]["name"], query["version"])
                results.append({"vulns": [{"id": vid} for vid in hits.get(key, [])]})
            return {"results": results}
        assert url.startswith(OSV_VULN)
        return records[url.removeprefix(OSV_VULN)]

    return fetch


@pytest.fixture
def tree(copy_fixture: Callable[[str], Path]) -> Path:
    root = copy_fixture("deps")
    (root / "tooling" / "checks").mkdir(parents=True)
    return root


def snapshot(root: Path, fetch: Callable[[str, bytes | None], Any] | None = None) -> None:
    packages = deps_audit.read_locks(root)
    advisories = query_osv(packages, fetch or fake_osv())
    write_snapshot(
        root / "tooling" / "checks" / "deps_audit_snapshot.json", root, packages, advisories
    )


def allow(root: Path, entries: list[dict[str, Any]], fail_at: str = "high") -> None:
    path = root / "tooling" / "checks" / "deps_audit_allow.json"
    path.write_text(json.dumps({"fail_at": fail_at, "allow": entries}), encoding="utf-8")


def entry(**overrides: str) -> dict[str, Any]:
    base = {
        "id": "GHSA-demo-0001",
        "package": "jinja2",
        "ecosystem": "PyPI",
        "expires": "2099-01-01",
        "reason": "only the build uses it, never with untrusted templates",
    }
    return {**base, **overrides}


def check(root: Path) -> deps_audit.Result:
    return deps_audit.run(Context(root=root))


# -- lock files ------------------------------------------------------------------------------------


def test_lock_parsers_read_both_ecosystems(tree: Path) -> None:
    uv = read_uv_lock(tree / "uv.lock")
    assert [(p.name, p.version, p.hashed) for p in uv] == [
        ("jinja2", "2.4.1", True),
        ("tinylib", "1.2.3", True),
    ]  # the editable project itself is not a dependency
    assert uv[0].uploaded == "2010-05-01T00:00:00Z"
    npm = read_pnpm_lock(tree / "pnpm-lock.yaml")
    assert [(p.name, p.version) for p in npm] == [
        ("@demo/scoped", "2.0.0"),
        ("lodash", "4.17.0"),
        ("pnpm", "12.9.1"),
    ]  # both YAML documents, scoped names, no snapshot duplicates
    assert all(p.source == "registry" and p.hashed for p in npm)


def test_real_locks_parse_with_hashes_from_their_registries() -> None:
    packages = deps_audit.read_locks(deps_audit.DEFAULT_ROOT)
    assert len([p for p in packages if p.ecosystem == "PyPI"]) > 100
    assert len([p for p in packages if p.ecosystem == "npm"]) > 300
    assert evaluate(packages, [], Policy(), TODAY).failures == []


# -- the guard -------------------------------------------------------------------------------------


def test_a_known_vulnerable_lock_fails(tree: Path) -> None:
    """ACC-S8-3: a locked version with a high advisory fails, naming the id and the fix."""
    snapshot(tree)
    result = check(tree)
    assert result.code == 1
    (line,) = result.violations
    assert "GHSA-demo-0001" in line and "jinja2@2.4.1" in line and "2.10.1" in line
    assert "PYSEC-demo-1" in line  # one finding per alias group
    assert any("lodash@4.17.0" in w and "[moderate]" in w for w in result.warnings)


def test_allow_listed_with_a_future_expiry_passes(tree: Path) -> None:
    snapshot(tree)
    allow(tree, [entry()])
    result = check(tree)
    assert result.code == 0, result.violations
    assert any(n.startswith("allowed until 2099-01-01") for n in result.notes)


def test_an_alias_id_matches_too(tree: Path) -> None:
    snapshot(tree)
    allow(tree, [entry(id="CVE-0000-0001")])
    assert check(tree).code == 0


def test_an_expired_entry_fails(tree: Path) -> None:
    snapshot(tree)
    allow(tree, [entry(expires="2020-01-01")])
    result = check(tree)
    assert result.code == 1
    assert any("expired on 2020-01-01" in v for v in result.violations)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"reason": ""}, "missing reason"),
        ({"reason": "fine"}, "the reason must say why"),
        ({"expires": "soon"}, "is not a YYYY-MM-DD date"),
        ({"package": ""}, "missing package"),
    ],
)
def test_a_malformed_entry_fails(tree: Path, change: dict[str, str], problem: str) -> None:
    snapshot(tree)
    allow(tree, [entry(**change)])
    result = check(tree)
    assert result.code == 1
    assert any(problem in v for v in result.violations), result.violations


def test_an_entry_for_another_package_does_not_cover_the_finding(tree: Path) -> None:
    snapshot(tree)
    allow(tree, [entry(package="tinylib")])
    result = check(tree)
    assert any("jinja2@2.4.1" in v for v in result.violations)
    assert any("matches no finding" in w for w in result.warnings)


def test_the_threshold_comes_from_the_allow_file(tree: Path) -> None:
    snapshot(tree)
    allow(tree, [entry()], fail_at="moderate")
    result = check(tree)
    assert result.code == 1
    assert any("lodash@4.17.0" in v for v in result.violations)


def test_an_unscored_advisory_fails_closed(tree: Path) -> None:
    records = {"PYSEC-only": {"id": "PYSEC-only", "summary": "no severity at all"}}
    hits = {("PyPI", "tinylib", "1.2.3"): ["PYSEC-only"]}
    snapshot(tree, fake_osv(hits, records))
    result = check(tree)
    assert any("PYSEC-only" in v and "[unknown]" in v for v in result.violations)


def test_findings_for_versions_no_longer_locked_are_ignored() -> None:
    old = Advisory("GHSA-demo-0001", "PyPI", "jinja2", "2.4.0", "critical")
    now = [Package("PyPI", "jinja2", "3.1.6")]
    assert evaluate(now, [old], Policy(), TODAY).failures == []


def test_provenance_git_path_and_unhashed_packages_fail() -> None:
    packages = [
        Package("PyPI", "fromgit", "1.0", source="git"),
        Package("npm", "tarred", "1.0", source="tarball"),
        Package("PyPI", "bare", "1.0", hashed=False),
    ]
    failures = evaluate(packages, [], Policy(), TODAY).failures
    assert len(failures) == 3
    assert "git source" in failures[0] and "tarball source" in failures[1]
    assert "without a hash" in failures[2]


def test_a_stale_snapshot_warns_and_a_missing_one_is_a_setup_error(tree: Path) -> None:
    assert check(tree).code == 2
    snapshot(tree)
    (tree / "uv.lock").write_text((tree / "uv.lock").read_text() + "\n", encoding="utf-8")
    result = check(tree)
    assert any("locks changed after the advisory snapshot" in w for w in result.warnings)


# -- sources ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vector", "score"),
    [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        (HIGH, 9.1),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N", 5.5),
        ("CVSS:3.0/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
        ("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N", None),
        ("CVSS:3.1/AV:N", None),
    ],
)
def test_cvss3_base_scores(vector: str, score: float | None) -> None:
    assert cvss3_score(vector) == score


def test_severity_prefers_the_database_label_then_cvss() -> None:
    assert osv_severity({"database_specific": {"severity": "MEDIUM"}}) == "moderate"
    assert osv_severity({"severity": [{"type": "CVSS_V3", "score": HIGH}]}) == "critical"
    assert osv_severity({"severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N"}]}) == "unknown"


def test_osv_queries_are_batched_and_paged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps_audit, "BATCH_SIZE", 2)
    packages = [Package("npm", f"p{i}", "1.0") for i in range(5)]
    seen: list[int] = []
    pages = {"p3": 2}

    def fetch(url: str, body: bytes | None) -> Any:
        if url == OSV_BATCH:
            queries = json.loads(body or b"{}")["queries"]
            seen.append(len(queries))
            out: list[dict[str, Any]] = []
            for query in queries:
                name = query["package"]["name"]
                left = pages.get(name, 0)
                if left:
                    pages[name] = left - 1
                    out.append({"vulns": [{"id": f"GHSA-page-{left}"}], "next_page_token": "t"})
                else:
                    out.append({"vulns": []})
            return {"results": out}
        vid = url.removeprefix(OSV_VULN)
        return {"id": vid, "database_specific": {"severity": "LOW"}}

    found = query_osv(packages, fetch)
    assert seen == [2, 2, 1, 1, 1]
    assert sorted(a.id for a in found) == ["GHSA-page-1", "GHSA-page-2"]


def test_a_short_batch_answer_is_unavailable_not_clean() -> None:
    with pytest.raises(AuditUnavailable):
        query_osv([Package("npm", "x", "1.0")], lambda url, body: {"results": []})


def test_pnpm_audit_rows() -> None:
    rows = parse_pnpm_audit(
        {
            "advisories": {
                "9": {
                    "id": 9,
                    "module_name": "vite",
                    "severity": "high",
                    "github_advisory_id": "GHSA-demo-vite",
                    "patched_versions": ">=8.3.3",
                    "title": "dev server issue",
                    "findings": [{"version": "8.3.2"}, {"version": "8.3.2"}],
                }
            }
        }
    )
    assert rows == [
        Advisory(
            id="GHSA-demo-vite",
            ecosystem="npm",
            package="vite",
            version="8.3.2",
            severity="high",
            aliases=("NPM-9",),
            summary="dev server issue",
            fixed=(">=8.3.3",),
            origin="pnpm-audit",
        )
    ]


def test_the_shipped_allow_file_is_well_formed() -> None:
    policy = read_policy()
    assert policy.problems == []
    assert policy.fail_at == "high"
    for item in policy.entries:
        assert item.expires > date(2026, 10, 20), item.id


# -- pinned downloads ------------------------------------------------------------------------------


def test_pinned_downloads_and_the_base_digest(tmp_path: Path) -> None:
    (tmp_path / "scripts" / "install").mkdir(parents=True)
    (tmp_path / "docker").mkdir()
    (tmp_path / "scripts" / "install" / "pins.json").write_text(
        json.dumps(
            {
                "uv": {
                    "targets": {
                        "a": {"url": "https://dl.example.invalid/uv-a", "mirror": "https://m/x"},
                        "b": {"url": "https://dl.example.invalid/uv-b"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "docker" / "pins.json").write_text(
        json.dumps(
            {
                "base": {"image": "debian", "digest": "sha256:" + "ab" * 32},
                "bundles": {
                    "tool": {
                        "version": "1.2",
                        "url": "https://dl.example.invalid/v{version}/{file}",
                        "files": {"x": {"file": "tool_{version}_x.tgz"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert deps_audit.pinned_urls(tmp_path) == [
        "https://dl.example.invalid/uv-a",
        "https://dl.example.invalid/uv-b",
        "https://dl.example.invalid/v1.2/tool_1.2_x.tgz",
    ]
    statuses = {"https://dl.example.invalid/uv-b": 404, "https://dl.example.invalid/uv-a": 429}
    asked: list[tuple[str, dict[str, str]]] = []

    def probe(url: str, headers: Any) -> int:
        asked.append((url, dict(headers)))
        return statuses.get(url, 200)

    problems, unverified = resolve_pins(tmp_path, probe, lambda url, body: {"token": "t0"})
    assert problems == ["pinned download https://dl.example.invalid/uv-b (404)"]
    assert unverified == ["pinned download https://dl.example.invalid/uv-a (429)"]
    manifest_url, headers = asked[-1]
    assert manifest_url.endswith("/v2/library/debian/manifests/sha256:" + "ab" * 32)
    assert headers["Authorization"] == "Bearer t0"
