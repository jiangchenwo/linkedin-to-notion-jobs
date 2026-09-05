import logging
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin"
REPO_DATA = Path(__file__).parents[1] / "data"


@pytest.fixture(autouse=True)
def _reset_logging():
    """Drop root-logger handlers between tests so a FileHandler bound to one
    test's temp dir does not leak into the next."""
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()
