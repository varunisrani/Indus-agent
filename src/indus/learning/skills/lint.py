"""Advisory skill linter: findings ride along in tool results and never block a write.

Rules (ids are stable; tests and the CLI ``indus skills check`` key on them):

- ``lint.incident-log``: the body reads like a history of tickets — at least ``INCIDENT_MIN_REFS``
  ``#123`` / ``PR 123`` / ``issue 123`` mentions in prose and at least ``INCIDENT_REFS_PER_KCHAR``
  per 1,000 characters of prose (fenced code is ignored). Skills should state rules, not stories.
- ``lint.sprawl``: more than ``MAX_REFERENCE_FILES`` files under ``references/``.
- ``lint.oversized``: a body longer than ``MAX_BODY_CHARS`` (it is loaded whole on every view).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from indus.learning.skills.format import SkillFormatError, split_frontmatter

__all__ = [
    "INCIDENT_MIN_REFS",
    "INCIDENT_REFS_PER_KCHAR",
    "MAX_BODY_CHARS",
    "MAX_REFERENCE_FILES",
    "LintFinding",
    "lint_skill",
    "lint_skill_dir",
]

INCIDENT_MIN_REFS = 5
INCIDENT_REFS_PER_KCHAR = 5.0
MAX_REFERENCE_FILES = 60
MAX_BODY_CHARS = 24_000

_FENCED = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_TICKET = re.compile(r"(?<![\w/&])#\d{2,7}\b|\b(?:PR|pr|issue|Issue)\s*#?\d{2,7}\b")


@dataclass(frozen=True)
class LintFinding:
    rule: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message}


def _body_of(text: str) -> str:
    try:
        _, body = split_frontmatter(text)
    except SkillFormatError:
        return text
    return body


def lint_skill(text: str, skill_dir: Path | None = None) -> list[LintFinding]:
    """Findings for ``SKILL.md`` text; ``skill_dir`` adds the on-disk checks."""
    findings: list[LintFinding] = []
    body = _body_of(text).strip()
    prose = _FENCED.sub("", body)
    refs = len(_TICKET.findall(prose))
    density = refs * 1000 / max(len(prose), 1)
    if refs >= INCIDENT_MIN_REFS and density >= INCIDENT_REFS_PER_KCHAR:
        findings.append(
            LintFinding(
                "lint.incident-log",
                f"{refs} ticket or PR numbers in the prose ({density:.1f} per 1,000 characters). "
                "State the lasting rule and why it holds; drop the incident history.",
            )
        )
    if len(body) > MAX_BODY_CHARS:
        findings.append(
            LintFinding(
                "lint.oversized",
                f"the body is {len(body):,} characters (advice: at most {MAX_BODY_CHARS:,}). It is "
                "loaded whole each time the skill is viewed; move depth into references/ files.",
            )
        )
    if skill_dir is not None:
        findings.extend(lint_skill_dir(skill_dir))
    return findings


def lint_skill_dir(skill_dir: Path) -> list[LintFinding]:
    refs_dir = skill_dir / "references"
    if not refs_dir.is_dir():
        return []
    count = sum(1 for p in refs_dir.rglob("*") if p.is_file())
    if count <= MAX_REFERENCE_FILES:
        return []
    return [
        LintFinding(
            "lint.sprawl",
            f"{count} files under references/ (advice: at most {MAX_REFERENCE_FILES}). Fold notes "
            "on the same topic into one file instead of adding one per session.",
        )
    ]
