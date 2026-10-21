"""Workspace verification (P24-17): ``recipes`` finds how to check a workspace, ``runner`` runs
the phases through the terminal backend. Used by ``indus verify`` and the verify-on-stop gate
(``core.stop_gates``)."""

from indus.tools.verify.recipes import Recipe, detect_recipe, package_manager
from indus.tools.verify.runner import (
    PHASE_TIMEOUT_S,
    PHASES,
    READY_TIMEOUT_S,
    PhaseResult,
    Readiness,
    VerifyResult,
    run_verify,
)

__all__ = [
    "PHASES",
    "PHASE_TIMEOUT_S",
    "READY_TIMEOUT_S",
    "PhaseResult",
    "Readiness",
    "Recipe",
    "VerifyResult",
    "detect_recipe",
    "package_manager",
    "run_verify",
]
