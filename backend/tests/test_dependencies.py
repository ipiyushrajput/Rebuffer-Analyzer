"""Version constraints that a fresh install must not be allowed to break.

A dependency whose own metadata is too loose can be resolved to a release that does not
work, and the failure lands at runtime on a deployment rather than here. Each constraint
below is pinned because a specific pairing is known to break; the test states which, so a
future widening is a decision rather than an accident.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def requirement(name: str) -> str:
    prefix = name.lower()
    for entry in dependencies():
        if entry.lower().startswith(prefix):
            return entry
    raise AssertionError(f"{name} is not a declared dependency")


def test_pymysql_is_held_below_the_release_aiomysql_cannot_import() -> None:
    """
    PyMySQL 1.1.2 removed `escape_bytes_prefixed`, which aiomysql imports at module scope.

    aiomysql declares only `PyMySQL>=1.0`, so a fresh install takes the newest release and
    every database call then raises `ImportError: cannot import name
    'escape_bytes_prefixed' from 'pymysql.converters'` — at the first query, not at install.
    The upper bound is what prevents that, so widening it has to break this test first.
    """
    assert requirement("pymysql") == "pymysql>=1.1,<1.1.2"


def test_the_async_mysql_driver_is_declared_alongside_it() -> None:
    """The pin above is only meaningful while aiomysql is the driver it protects."""
    assert requirement("aiomysql").startswith("aiomysql>=")


@pytest.mark.parametrize("name", ["sqlalchemy", "alembic", "aiosqlite", "openpyxl"])
def test_every_storage_dependency_is_declared(name: str) -> None:
    """A missing one of these fails at startup on a deployment and nowhere earlier."""
    assert requirement(name)
