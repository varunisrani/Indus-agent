"""The page outline the browser tools show the model, and how a long one is cut or condensed.

The page is walked in the browser itself (``OUTLINE_JS``): every visible interactive element gets
a ref (``e1``, ``e2`` …) written into a per-session attribute, so a later click or type finds the
same element and a page cannot forge a ref of its own (it does not know the attribute's name).
The walker returns flat rows ``{depth, role, name, ref?, value?, level?, checked?, disabled?}``;
``render`` turns them into indented lines such as ``- button "Send" [ref=e3]``.

The compact view keeps interactive elements and headings; the full view adds the visible text.
An outline longer than the threshold is cut at a line boundary. The whole outline (redacted, at
most ``MAX_STORED_CHARS``) is saved under ``<home>/cache/web/`` and the note names the
``read_file`` call that shows the rest; with ``browser.snapshot_summary`` and an auxiliary model
the cut part is replaced by the model's summary instead (the saved copy stays).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output

__all__ = [
    "DEFAULT_THRESHOLD",
    "IMAGES_JS",
    "LABELS_JS",
    "MAX_STORED_CHARS",
    "MIN_THRESHOLD",
    "OUTLINE_JS",
    "UNLABEL_JS",
    "Outline",
    "clamp_threshold",
    "fit",
    "redact_value",
    "render",
    "store_outline",
]

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 15_000
MIN_THRESHOLD = 1_000
MAX_STORED_CHARS = 2_000_000
STORE_DIR = Path("cache") / "web"
READ_WINDOW = 200
MAX_REDACT_DEPTH = 32
DEPTH_MARKER = "[nested too deep]"
SUMMARY_TASK = "browser_snapshot"
SUMMARY_MAX_TOKENS = 2_000

# -- in-page scripts ------------------------------------------------------------------------------

OUTLINE_JS = r"""
(args) => {
  const attr = args.attr;
  const full = !!args.full;
  for (const old of document.querySelectorAll('[' + attr + ']')) old.removeAttribute(attr);
  const ACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio',
    'combobox', 'listbox', 'option', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'slider',
    'spinbutton', 'switch', 'tab', 'treeitem']);
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK']);
  const LEVELS = {H1: 1, H2: 2, H3: 3, H4: 4, H5: 5, H6: 6};
  const FIELDS = new Set(['textbox', 'searchbox', 'combobox', 'spinbutton']);
  const squash = (t, n) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n || 160);
  const inputRole = (el) => {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'hidden') return null;
    if (t === 'checkbox' || t === 'radio') return t;
    if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
    if (t === 'range') return 'slider';
    if (t === 'number') return 'spinbutton';
    if (t === 'search') return 'searchbox';
    return 'textbox';
  };
  const roleOf = (el) => {
    const given = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
    if (given) return given;
    const tag = el.tagName;
    if (tag === 'INPUT') return inputRole(el);
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'IMG') return 'img';
    if (LEVELS[tag]) return 'heading';
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) {
      return 'textbox';
    }
    return null;
  };
  // P24-40: a field holding a secret (a password, card data, a one-time code, or a value
  // browser_vault filled) never shows its value in an outline.
  const secretField = (el) => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    return t === 'password' || ac.startsWith('cc-') || ac === 'one-time-code' ||
      el.hasAttribute('data-indus-filled');
  };
  const shown = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const byIds = (ids) => ids.split(/\s+/).map((id) => {
    const found = document.getElementById(id);
    return found ? found.innerText || found.textContent : '';
  }).join(' ');
  const nameOf = (el, role) => {
    const label = el.getAttribute('aria-label');
    if (label) return squash(label);
    const by = el.getAttribute('aria-labelledby');
    if (by) { const text = squash(byIds(by)); if (text) return text; }
    if (el.labels && el.labels.length) {
      const text = squash(Array.from(el.labels).map((l) => l.innerText).join(' '));
      if (text) return text;
    }
    if (el.tagName === 'IMG') return squash(el.getAttribute('alt'));
    if (el.tagName === 'INPUT' && role === 'button') {
      return squash(el.value || el.getAttribute('alt'));
    }
    if (FIELDS.has(role)) {
      return squash(el.getAttribute('placeholder') || el.getAttribute('title'));
    }
    return squash(el.innerText || el.textContent || el.getAttribute('title'));
  };
  const rows = [];
  let count = 0;
  const walk = (parent, depth, named) => {
    for (const el of parent.children) {
      if (SKIP.has(el.tagName) || el.hidden || el.getAttribute('aria-hidden') === 'true') continue;
      const visible = shown(el);
      const role = roleOf(el);
      let emitted = false;
      if (visible && role && (ACTIVE.has(role) || role === 'heading' || (full && role === 'img'))) {
        const row = {depth: depth, role: role, name: nameOf(el, role)};
        if (ACTIVE.has(role)) {
          count += 1;
          row.ref = 'e' + count;
          el.setAttribute(attr, row.ref);
          if ('value' in el && (FIELDS.has(role) || role === 'slider')) {
            const raw = String(el.value);
            row.value = secretField(el) ? (raw ? '[hidden]' : '') : squash(raw, 200);
          }
          if (role === 'checkbox' || role === 'radio' || role === 'switch') {
            row.checked = !!el.checked || el.getAttribute('aria-checked') === 'true';
          }
          if (el.disabled || el.getAttribute('aria-disabled') === 'true') row.disabled = true;
        }
        if (role === 'heading') {
          row.level = LEVELS[el.tagName] || Number(el.getAttribute('aria-level')) || 2;
        }
        rows.push(row);
        emitted = true;
        if (ACTIVE.has(role) && role !== 'listbox') continue;
      }
      if (full && visible && !named && !emitted) {
        for (const node of el.childNodes) {
          if (node.nodeType !== 3) continue;
          const text = squash(node.textContent, 400);
          if (text) rows.push({depth: depth, role: 'text', name: text});
        }
      }
      walk(el, emitted ? depth + 1 : depth, named || emitted);
    }
  };
  const root = document.body || document.documentElement;
  if (root) walk(root, 0, false);
  return rows;
}
"""

IMAGES_JS = r"""
() => Array.from(document.images)
  .map((img) => ({src: img.currentSrc || img.src || '', alt: img.alt || '',
    width: img.naturalWidth, height: img.naturalHeight}))
  .filter((img) => img.src && !img.src.startsWith('data:'))
"""

LABELS_JS = r"""
(attr) => {
  const marks = [];
  for (const el of document.querySelectorAll('[' + attr + ']')) {
    const box = el.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) continue;
    const ref = el.getAttribute(attr);
    const tag = document.createElement('div');
    tag.setAttribute('data-indus-label', '1');
    tag.textContent = '[' + ref.slice(1) + ']';
    tag.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;' +
      'background:#ffd400;color:#000;font:bold 11px monospace;padding:0 2px;' +
      'border:1px solid #000;' +
      'left:' + Math.max(0, box.left) + 'px;top:' + Math.max(0, box.top - 12) + 'px';
    document.documentElement.appendChild(tag);
    marks.push(ref);
  }
  return marks;
}
"""

UNLABEL_JS = r"""
() => { for (const tag of document.querySelectorAll('[data-indus-label]')) tag.remove(); }
"""

# -- rendering ------------------------------------------------------------------------------------


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _row_line(row: Mapping[str, Any]) -> str:
    depth = row.get("depth")
    indent = "  " * min(depth if isinstance(depth, int) else 0, 20)
    role = str(row.get("role") or "generic")
    name = str(row.get("name") or "")
    if role == "text":
        return f"{indent}- text: {name}"
    parts = [f"{indent}- {role}"]
    if name:
        parts.append(_quoted(name))
    if row.get("level") is not None and role == "heading":
        parts.append(f"[level={row['level']}]")
    if row.get("ref"):
        parts.append(f"[ref={row['ref']}]")
    if row.get("checked") is True:
        parts.append("checked")
    if row.get("disabled") is True:
        parts.append("disabled")
    value = row.get("value")
    if isinstance(value, str) and value:
        parts.append(f"value={_quoted(value)}")
    return " ".join(parts)


def render(rows: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    """``(outline text, number of refs)`` for the walker's rows."""
    lines = [_row_line(row) for row in rows]
    refs = sum(1 for row in rows if row.get("ref"))
    if not lines:
        return "(the page shows no headings or interactive elements)", 0
    return "\n".join(lines), refs


# -- redaction ------------------------------------------------------------------------------------


def redact_value(value: Any, scope: Scope | None = None, _depth: int = 0) -> Any:
    """Secrets masked in any text inside ``value`` (page text is shown to the model)."""
    if _depth >= MAX_REDACT_DEPTH:
        return DEPTH_MARKER
    if isinstance(value, str):
        return redact_output(value, force=True, scope=scope)
    if isinstance(value, list | tuple):
        items = cast(Sequence[Any], value)
        return [redact_value(item, scope, _depth + 1) for item in items]
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {
            redact_value(key, scope, _depth + 1): redact_value(item, scope, _depth + 1)
            for key, item in mapping.items()
        }
    return value


# -- long outlines --------------------------------------------------------------------------------


def clamp_threshold(value: object) -> int:
    if value is None or isinstance(value, bool):
        return DEFAULT_THRESHOLD
    try:
        number = int(cast(Any, value))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return max(MIN_THRESHOLD, number)


def store_outline(home: Path, text: str, scope: Scope | None = None) -> Path | None:
    """Save the whole outline (redacted, bounded); ``None`` when the write fails."""
    content = redact_output(text, force=True, scope=scope)
    if len(content) > MAX_STORED_CHARS:
        content = (
            content[:MAX_STORED_CHARS]
            + f"\n\n[saved copy cut at {MAX_STORED_CHARS:,} of {len(content):,} characters]"
        )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    path = home / STORE_DIR / f"browser-snapshot-{digest}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
    except OSError as exc:
        log.warning("could not save the full page outline: %s", exc)
        return None
    return path


class AuxCaller(Protocol):
    async def call(self, task: str, messages: Sequence[Any], **options: Any) -> Any: ...


@dataclass(frozen=True)
class Outline:
    text: str
    refs: int
    truncated: bool = False
    saved_path: str | None = None
    summarised: bool = False


_SUMMARY_PROMPT = (
    "Below is a text outline of a web page. Lines carry roles, names and refs such as [ref=e12]. "
    "Write a shorter outline for an agent that will act on the page: keep every interactive "
    "element that looks useful with its exact ref, keep the headings, and summarise long runs "
    "of text in a sentence each. Answer with the outline only."
)


async def _summarise(aux: AuxCaller, text: str) -> str | None:
    messages = [
        {"role": "system", "content": _SUMMARY_PROMPT},
        {"role": "user", "content": text},
    ]
    try:
        result = await aux.call(SUMMARY_TASK, messages, max_tokens=SUMMARY_MAX_TOKENS)
    except Exception as exc:
        log.info("outline summary failed (%s); cutting instead", type(exc).__name__)
        return None
    answer = getattr(result, "text", result)
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


def _cut(text: str, limit: int, path: Path | None) -> str:
    note_room = min(160 + len(str(path or "")), limit // 2)
    kept: list[str] = []
    used = 0
    lines = text.split("\n")
    for line in lines:
        if used + len(line) + 1 > limit - note_room:
            break
        kept.append(line)
        used += len(line) + 1
    left = len(lines) - len(kept)
    if path is not None:
        note = (
            f"[{left} more lines not shown. Full outline: read_file "
            f'path="{path}" offset={len(kept) + 1} limit={READ_WINDOW}]'
        )
    else:
        note = f"[{left} more lines not shown; call browser_snapshot again to see them]"
    return "\n".join([*kept, "", note])


async def fit(
    text: str,
    refs: int,
    *,
    threshold: int,
    home: Path | None,
    scope: Scope | None = None,
    aux: AuxCaller | None = None,
    summarise: bool = False,
) -> Outline:
    """The outline as the model gets it: whole when short, else summarised or cut."""
    if len(text) <= threshold:
        return Outline(text, refs)
    path = store_outline(home, text, scope) if home is not None else None
    shown = str(path) if path is not None else None
    if summarise and aux is not None:
        summary = await _summarise(aux, text)
        if summary is not None:
            if len(summary) > threshold:
                summary = summary[:threshold]
            where = f' Full outline: read_file path="{path}".' if path is not None else ""
            body = f"{summary}\n\n[Summary of a {len(text):,}-character outline.{where}]"
            return Outline(body, refs, True, shown, True)
    return Outline(_cut(text, threshold, path), refs, True, shown)
