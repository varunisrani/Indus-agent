"""PR body: must carry a ``Reference:`` line (files read) and a ``Task note:`` line.

Either value may be ``none (<reason>)``. Body sources, first found wins: ``--body-file``, env
``PR_BODY``, the ``pull_request.body`` of the event file named by ``GITHUB_EVENT_PATH``.
Without any source (a local run) the check is skipped.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from tooling.checks.common import Context, Result

NAME = "prbody"
FIELDS = ("Reference", "Task note")
NONE_OK = re.compile(r"^none\s*\(\s*\S.*\)$", re.IGNORECASE)


def field_value(body: str, label: str) -> str | None:
    pattern = re.compile(
        rf"^\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(label)}:(?:\*\*)?[ \t]*(?P<v>.*)$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(body)
    return None if m is None else m.group("v").strip().strip("*").strip()


def check_body(body: str) -> list[str]:
    problems = []
    for label in FIELDS:
        value = field_value(body, label)
        if value is None:
            problems.append(f"missing a '{label}:' line")
        elif not value:
            problems.append(f"'{label}:' is empty; name the files, or write 'none (<reason>)'")
        elif value.lower().startswith("none") and not NONE_OK.match(value):
            problems.append(f"'{label}: {value}' needs a reason: 'none (<reason>)'")
    return problems


def find_body(ctx: Context) -> tuple[str | None, str]:
    if ctx.body_file:
        return Path(ctx.body_file).read_text(encoding="utf-8"), ctx.body_file
    if "PR_BODY" in os.environ:
        return os.environ["PR_BODY"], "$PR_BODY"
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and Path(event).is_file():
        data = json.loads(Path(event).read_text(encoding="utf-8"))
        pr = data.get("pull_request") if isinstance(data, dict) else None
        if isinstance(pr, dict):
            return pr.get("body") or "", "$GITHUB_EVENT_PATH"
    return None, ""


def run(ctx: Context) -> Result:
    result = Result(NAME)
    try:
        body, source = find_body(ctx)
    except (OSError, json.JSONDecodeError) as exc:
        result.setup(f"cannot read the PR body: {exc}")
        return result
    if body is None:
        result.notes.append("no PR body given (--body-file, PR_BODY, GITHUB_EVENT_PATH); skipped")
        return result
    for problem in check_body(body):
        result.fail(f"PR body ({source}): {problem}")
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
