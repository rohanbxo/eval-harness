"""Shared pytest fixtures.

Keep this file small and generic: anything specific to one area of the harness
belongs in that area's test module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR / "data"
BACKEND_DIR = TESTS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent


@pytest.fixture(scope="session")
def tests_dir() -> Path:
    """The ``backend/tests`` directory."""
    return TESTS_DIR


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """Static test inputs: ``tests/data/ok/`` and ``tests/data/broken/``."""
    return DATA_DIR


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root, which holds ``scenarios/`` and ``config/``."""
    return REPO_ROOT
