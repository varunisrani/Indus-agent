"""The shared base for every protocol model."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import sys
import threading
from pathlib import Path
from typing import Annotated, Any, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError

TEXT_EMPTY_ERROR = "text_empty"
"""The pydantic error type raised for blank message text."""


def _not_blank(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError(TEXT_EMPTY_ERROR, "text must not be empty")
    return value


type MessageText = Annotated[str, AfterValidator(_not_blank)]
"""Message text: any string with at least one non-whitespace character."""

type SessionId = Annotated[str, Field(min_length=1)]
"""A session id."""


DOCS_TWIN = Path(__file__).with_name("field_docs.json")
"""Generated field docstrings of the wire models (``python -m indus.protocol.field_docs``)."""

_DOCS_LOCK = threading.Lock()
_DOCS: dict[str, dict[int, dict[str, str]]] = {}
_twin: dict[str, Any] | None = None


def class_docs(tree: ast.AST) -> dict[int, dict[str, str]]:
    """Attribute docstrings of every class in ``tree``, keyed by the class's first line.

    The same rule as pydantic's ``use_attribute_docstrings``: a string expression right after an
    annotated assignment to a plain name documents that name.
    """
    found: dict[int, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        docs: dict[str, str] = {}
        previous: ast.stmt | None = None
        for stmt in node.body:
            if (
                isinstance(previous, ast.AnnAssign)
                and isinstance(previous.target, ast.Name)
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                docs[previous.target.id] = inspect.cleandoc(stmt.value.value)
            previous = stmt
        found[min([node.lineno, *(d.lineno for d in node.decorator_list)])] = docs
        found.setdefault(node.lineno, docs)
    return found


def source_digest(data: bytes) -> str:
    """The sha256 of a module's source bytes, as the twin records it."""
    return hashlib.sha256(data).hexdigest()


def _twin_modules() -> dict[str, Any]:
    global _twin
    if _twin is None:
        modules: object = None
        try:
            loaded: object = json.loads(DOCS_TWIN.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                modules = cast(dict[str, Any], loaded).get("modules")
        except (OSError, ValueError):
            modules = None
        _twin = cast(dict[str, Any], modules) if isinstance(modules, dict) else {}
    return _twin


def module_docs(module: str, filename: str) -> dict[int, dict[str, str]]:
    """Field docstrings of every class in one module: from the twin while it names this source's
    digest, else from one parse of the source (W48-g-2)."""
    try:
        data = Path(filename).read_bytes()
    except OSError:
        return {}
    entry = _twin_modules().get(module)
    if isinstance(entry, dict):
        fresh = cast(dict[str, Any], entry)
        if fresh.get("sha256") == source_digest(data):
            classes = cast(dict[str, dict[str, str]], fresh.get("classes") or {})
            return {int(line): docs for line, docs in classes.items()}
    try:
        return class_docs(ast.parse(data))
    except SyntaxError:
        return {}


def attribute_docstrings(cls: type) -> dict[str, str]:
    """``cls``'s own field docstrings (what pydantic's ``use_attribute_docstrings`` would find).

    Pydantic re-reads and re-parses the source of every class (~55 ms at import for the ~240 wire
    models); here each module is read once and its parse comes from the generated twin.
    """
    module = sys.modules.get(cls.__module__)
    filename = getattr(module, "__file__", None)
    line = getattr(cls, "__firstlineno__", None)
    if not filename or not isinstance(line, int):
        return {}
    with _DOCS_LOCK:
        table = _DOCS.get(filename)
        if table is None:
            table = _DOCS[filename] = module_docs(cls.__module__, filename)
    return table.get(line, {})


class ProtocolModel(BaseModel):
    """Base of every wire model: unknown keys are refused and field docstrings become schema text.

    ``to_wire`` is the one way a model becomes a JSON-ready dict. Optional fields left at ``None``
    are dropped so frames stay small; a subclass that must keep a ``null`` (a JSON-RPC result)
    overrides it.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        # No use_attribute_docstrings: __pydantic_init_subclass__ sets the same descriptions from
        # the generated field_docs.json twin instead of parsing source per class (W48-g-2).
        validate_default=True,
        # Build each validator on first use, not at import: a one-shot run touches a handful of
        # the ~300 wire models, and building them all cost ~150 ms of startup (W48-e-1).
        defer_build=True,
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        docs = attribute_docstrings(cls)
        if not docs:
            return
        fields = cls.__pydantic_fields__
        for name, info in fields.items():
            if info.description is None and name in docs:
                info.description = docs[name]
        if cls.__pydantic_complete__:
            # Built already (not deferred): rebuild so the schema carries the descriptions.
            cls.model_rebuild(force=True)

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-ready form of this model."""
        return self.model_dump(mode="json", exclude_none=True)
