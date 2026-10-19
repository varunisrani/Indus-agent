"""``browser_vision``: a screenshot of the current page, looked at by a model.

The screenshot is saved under ``<home>/cache/screenshots/`` (files older than a day are removed,
at most once an hour). With ``annotate`` the elements that have refs get ``[N]`` labels first
(``[N]`` is ref ``@eN``). When the main model takes images (the host offers ``native_vision=True``)
the image goes back as a multimodal result and the model looks at it itself on its next step;
otherwise the auxiliary ``vision`` task answers the question about the image and its answer
(redacted: a screenshot can show secrets) comes back as text. Both carry the screenshot path,
which can be shown to the user as ``MEDIA:<path>``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.tools.builtin.browser.tools import PageTools, as_json, fail

__all__ = ["SCHEMA", "SCREENSHOT_DIR", "VisionTool"]

log = logging.getLogger(__name__)

SCREENSHOT_DIR = Path("cache") / "screenshots"
KEEP_SECONDS = 24 * 3600.0
PRUNE_EVERY_SECONDS = 3600.0
VISION_TASK = "vision"
VISION_MAX_TOKENS = 1_500
VISION_TEMPERATURE = 0.1

_PROMPT = (
    "This is a screenshot of a web page that an agent is working on. Answer the agent's "
    "question about it. Describe what you see that matters for the question: visible text, "
    "buttons, fields, error messages, layout. If the page shows a CAPTCHA or another check, "
    "say what kind it is. Question: {question}"
)


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


class VisionTool:
    def __init__(self, pages: PageTools) -> None:
        self.pages = pages
        self._pruned: dict[Path, float] = {}

    def _prune(self, folder: Path) -> None:
        now = time.time()
        if now - self._pruned.get(folder, 0.0) < PRUNE_EVERY_SECONDS:
            return
        self._pruned[folder] = now
        for old in folder.glob("browser-*.png"):
            with contextlib.suppress(OSError):
                if now - old.stat().st_mtime > KEEP_SECONDS:
                    old.unlink()

    async def __call__(
        self,
        args: Mapping[str, Any],
        *,
        task_id: str | None = None,
        scope: Scope | None = None,
        aux: Any = None,
        native_vision: bool | None = None,
    ) -> str | dict[str, Any]:
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            return fail("question is required: say what you want to know about the page")
        annotate = bool(args.get("annotate"))
        session = await self.pages.open_session(task_id, scope)
        if isinstance(session, str):
            return session
        bad = await self.pages.landed_badly(session, "the page is on")
        if bad is not None:
            return bad
        folder = session.scope.home / SCREENSHOT_DIR
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return fail(f"The screenshot folder could not be made: {exc}")
        self._prune(folder)
        path = folder / f"browser-{secrets.token_hex(8)}.png"
        attr = session.ref_attr if annotate else None
        try:
            if annotate:
                await self.pages.run(
                    session, lambda p: p.outline(False, session.ref_attr), label="snapshot"
                )
            labels = await self.pages.run(
                session, lambda p: p.screenshot(path, label_attr=attr), label="screenshot"
            )
        except Exception as exc:
            return self.pages.failure(exc, session, "Taking the screenshot")
        if not path.is_file():
            return fail("The browser reported a screenshot but no file was written.")
        url = redact_output(session.page.url(), force=True, scope=session.scope)
        extra: dict[str, Any] = {}
        if annotate:
            extra["annotations"] = [f"[{ref[1:]}] = @{ref}" for ref in labels]
        if native_vision:
            return self._envelope(path, question, url, extra)
        if aux is None:
            return as_json(
                {
                    "success": False,
                    "error": "No model that can look at images is set up for this session "
                    "(the main model takes no images and there is no auxiliary vision model).",
                    "screenshot_path": str(path),
                    **extra,
                }
            )
        try:
            answer = await self._ask(aux, path, question)
        except Exception as exc:
            log.warning("browser_vision analysis failed: %s", exc)
            return as_json(
                {
                    "success": False,
                    "error": f"The vision model failed: {type(exc).__name__}: {exc}",
                    "screenshot_path": str(path),
                    "note": "The screenshot was saved; it can still be shared as MEDIA:<path>.",
                    **extra,
                }
            )
        return as_json(
            {
                "success": True,
                "analysis": redact_output(answer, force=True, scope=session.scope)
                or "The vision model gave no answer.",
                "screenshot_path": str(path),
                "url": url,
                **extra,
            }
        )

    @staticmethod
    def _envelope(path: Path, question: str, url: str, extra: Mapping[str, Any]) -> dict[str, Any]:
        summary = (
            f"Screenshot of {url} attached for: {question.strip()} "
            f"(saved at {path}; share it as MEDIA:{path})."
        )
        if extra.get("annotations"):
            summary += " Labels: " + ", ".join(extra["annotations"])
        return {
            "multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url", "image_url": {"url": _data_url(path)}},
            ],
            "text_summary": summary,
        }

    @staticmethod
    async def _ask(aux: Any, path: Path, question: str) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT.format(question=question.strip())},
                    {"type": "image_url", "image_url": {"url": _data_url(path)}},
                ],
            }
        ]
        result = await aux.call(
            VISION_TASK, messages, max_tokens=VISION_MAX_TOKENS, temperature=VISION_TEMPERATURE
        )
        text = getattr(result, "text", result)
        return text.strip() if isinstance(text, str) else json.dumps(text, default=str)


SCHEMA: dict[str, Any] = {
    "name": "browser_vision",
    "description": (
        "Take a screenshot of the current browser page and look at it, for what the text "
        "outline cannot show: layout, images, charts, CAPTCHAs, visual errors. Ask a precise "
        "question. When your model can see images the screenshot is attached for you to look "
        "at; otherwise a vision model answers the question. The result includes "
        "screenshot_path; write MEDIA:<screenshot_path> in your reply to show the user the "
        "image. Open a page with browser_navigate first."
    ),
    "parameters": {
        "type": "object",
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "description": "What you want to know about the page, as precisely as you can.",
            },
            "annotate": {
                "type": "boolean",
                "default": False,
                "description": "Put [N] labels on interactive elements; [N] is ref @eN.",
            },
        },
    },
}
