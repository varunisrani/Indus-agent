"""The shared JSON-RPC layer every backend transport uses (P17-12): ``dispatch``."""

from indus.service.rpc.dispatch import (
    DEADLINES,
    DEFAULT_DEADLINE_S,
    ContractViolation,
    Dispatcher,
    Handler,
    MethodSet,
    MethodTable,
    RpcContext,
    UndeclaredMethod,
    check_event,
    check_payload,
    declared,
    decode_request,
    error_frame,
    strict_from,
)

__all__ = [
    "DEADLINES",
    "DEFAULT_DEADLINE_S",
    "ContractViolation",
    "Dispatcher",
    "Handler",
    "MethodSet",
    "MethodTable",
    "RpcContext",
    "UndeclaredMethod",
    "check_event",
    "check_payload",
    "decode_request",
    "declared",
    "error_frame",
    "strict_from",
]
