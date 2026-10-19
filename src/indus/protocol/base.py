"""The shared base for every protocol model."""

from __future__ import annotations

from typing import Annotated, Any

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


class ProtocolModel(BaseModel):
    """Base of every wire model: unknown keys are refused and field docstrings become schema text.

    ``to_wire`` is the one way a model becomes a JSON-ready dict. Optional fields left at ``None``
    are dropped so frames stay small; a subclass that must keep a ``null`` (a JSON-RPC result)
    overrides it.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        validate_default=True,
    )

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-ready form of this model."""
        return self.model_dump(mode="json", exclude_none=True)
