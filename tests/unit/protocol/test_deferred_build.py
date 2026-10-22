"""W48-e-1: wire models build their validators on first use and still check like before."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from indus.protocol.base import ProtocolModel


def test_a_new_wire_model_is_built_on_first_use_and_refuses_unknown_keys() -> None:
    class Probe(ProtocolModel):
        name: str
        """A name."""

    assert Probe.__pydantic_complete__ is False
    assert Probe(name="x").to_wire() == {"name": "x"}
    assert Probe.__pydantic_complete__ is True
    with pytest.raises(ValidationError):
        Probe.model_validate({"name": "x", "extra": 1})
    with pytest.raises(ValidationError):
        Probe(name="x").name = "y"  # type: ignore[misc]  # frozen
    assert Probe.model_json_schema()["properties"]["name"]["description"] == "A name."
