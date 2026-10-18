"""The shared base for every protocol model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


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
