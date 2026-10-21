"""The image lanes (P23-08, P23-09): run against images built beforehand, marker ``docker``.

The Docker workflow builds each image, then runs ``pytest -m docker tests/docker`` with
``INDUS_TEST_IMAGE`` (product) or ``INDUS_TEST_SANDBOX_IMAGE`` (sandbox) naming it and
``INDUS_TEST_IMAGE_VARIANT`` (``slim`` / ``browser``). Without them, or without a reachable daemon,
every test here skips: the lanes never build or pull on their own. The variables are read at
import, before the test fixtures drop every inherited ``INDUS_*`` variable.
"""

from __future__ import annotations

import os

import pytest

from tests.integration.backends._live import docker_problem

PRODUCT_IMAGE = os.environ.get("INDUS_TEST_IMAGE", "")
SANDBOX_IMAGE = os.environ.get("INDUS_TEST_SANDBOX_IMAGE", "")
VARIANT = os.environ.get("INDUS_TEST_IMAGE_VARIANT", "slim")


def need(image: str, variable: str) -> str:
    """The image to test, or skip with the reason."""
    if not image:
        pytest.skip(f"{variable} names no image (the Docker workflow sets it after a build)")
    problem = docker_problem(image)
    if problem:
        pytest.skip(problem)
    return image


@pytest.fixture
def product_image() -> str:
    return need(PRODUCT_IMAGE, "INDUS_TEST_IMAGE")


@pytest.fixture
def sandbox_image() -> str:
    return need(SANDBOX_IMAGE, "INDUS_TEST_SANDBOX_IMAGE")
