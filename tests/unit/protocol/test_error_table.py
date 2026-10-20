"""The single protocol error-code table (D-16.27, trap R05.15)."""

from __future__ import annotations

import re

import pytest

from indus.foundation.errors import ERROR_CODES as INDUS_CODES
from indus.protocol import errors
from indus.protocol.envelope import ErrorDetail
from indus.protocol.errors import (
    BY_CODE,
    BY_NAME,
    COMMON_ERRORS,
    ERROR_TABLE,
    GROUP_STATUS,
    ErrorCode,
    ErrorGroup,
    error_for_indus_code,
    make_error,
)
from indus.protocol.methods import METHODS

# Our own codes: one band of a thousand per group, read from the number.
BANDS = {
    ErrorGroup.INVALID_REQUEST: 1,
    ErrorGroup.NOT_FOUND: 2,
    ErrorGroup.CONFLICT: 3,
    ErrorGroup.REFUSED: 4,
    ErrorGroup.PROVIDER: 5,
    ErrorGroup.INTERNAL: 6,
}


def test_codes_and_names_are_unique() -> None:
    codes = [e.code for e in ERROR_TABLE]
    names = [e.name for e in ERROR_TABLE]
    assert len(set(codes)) == len(codes)
    assert len(set(names)) == len(names)
    assert len(BY_CODE) == len(BY_NAME) == len(ERROR_TABLE)


def test_every_module_level_code_is_in_the_table() -> None:
    declared = {v for v in vars(errors).values() if isinstance(v, ErrorCode)}
    assert declared == set(ERROR_TABLE)


def test_names_are_upper_snake_and_meanings_are_set() -> None:
    for e in ERROR_TABLE:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", e.name), e.name
        assert e.meaning and e.meaning == e.meaning.strip()


def test_every_group_has_a_status_and_is_used() -> None:
    assert set(GROUP_STATUS) == set(ErrorGroup)
    assert {e.group for e in ERROR_TABLE} == set(ErrorGroup)
    for e in ERROR_TABLE:
        assert e.status == GROUP_STATUS[e.group]


def test_jsonrpc_spec_codes_keep_their_numbers() -> None:
    assert errors.PARSE_ERROR.code == -32700
    assert errors.INVALID_REQUEST.code == -32600
    assert errors.METHOD_NOT_FOUND.code == -32601
    assert errors.INVALID_PARAMS.code == -32602
    assert errors.INTERNAL_ERROR.code == -32603


def test_our_codes_sit_in_their_group_band() -> None:
    for e in ERROR_TABLE:
        if e.code < 0:
            assert -32768 <= e.code <= -32000, e.name
            assert not -32099 <= e.code <= -32000, "server-error band is unused"
            continue
        assert e.code // 1000 == BANDS[e.group], e.name


def test_every_code_is_declared_by_a_method() -> None:
    declared = {e for spec in METHODS.values() for e in spec.errors}
    out_of_band = set(errors.OUT_OF_BAND_ERRORS)
    assert declared | out_of_band == set(ERROR_TABLE)
    assert not declared & out_of_band  # a code either answers a method or travels elsewhere
    assert all(where for where in errors.OUT_OF_BAND_ERRORS.values())


def test_every_method_declares_the_common_codes_once() -> None:
    for spec in METHODS.values():
        assert spec.errors[: len(COMMON_ERRORS)] == COMMON_ERRORS
        assert len(set(spec.errors)) == len(spec.errors), spec.name


@pytest.mark.parametrize(
    ("indus_code", "expected"),
    [
        ("store.session_not_found", errors.SESSION_NOT_FOUND),
        ("store.tombstoned", errors.SESSION_REFUSED),
        ("store.compression_closed", errors.SESSION_REFUSED),
        ("store.session_guarded", errors.SESSION_BUSY),
        ("store.turn_lease_lost", errors.LEASE_LOST),
        ("runtime.stale_generation", errors.STALE_GENERATION),
        ("runtime.admission_conflict", errors.ADMISSION_CONFLICT),
        ("runtime.queue_full", errors.QUEUE_FULL),
        ("runtime.unknown_execution", errors.OUTCOME_UNKNOWN),
        ("runtime.policy_conflict", errors.POLICY_CONFLICT),
        ("runtime.not_found", errors.ADMISSION_NOT_FOUND),
        ("store.locked", errors.STORE_FAILED),
        ("provider.no_usable", errors.PROVIDER_UNAVAILABLE),
        ("config.error", errors.INTERNAL_ERROR),
        ("never.heard_of_it", errors.INTERNAL_ERROR),
    ],
)
def test_product_codes_map_onto_the_table(indus_code: str, expected: ErrorCode) -> None:
    assert error_for_indus_code(indus_code) is expected


def test_exact_mappings_name_real_product_codes() -> None:
    for code in errors._INDUS_EXACT:  # pyright: ignore[reportPrivateUsage]
        assert code in INDUS_CODES, code


def test_make_error_fills_message_and_detail() -> None:
    obj = make_error(errors.INVALID_PARAMS, path="text")
    assert obj.code == -32602
    assert obj.message == errors.INVALID_PARAMS.meaning
    assert obj.data == {"name": "INVALID_PARAMS", "path": "text"}
    obj = make_error(errors.STORE_FAILED, "disk full", indus_code="store.locked", retryable=True)
    assert obj.message == "disk full"
    assert ErrorDetail.model_validate(obj.data).retryable is True
