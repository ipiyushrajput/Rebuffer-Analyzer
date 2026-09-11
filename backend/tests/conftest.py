from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Tests run against a local SQLite file, never the deployment database. Environment
# variables take priority over backend/.env in pydantic-settings, so this wins.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="rba-tests-"))
os.environ["DB_ENGINE"] = "sqlite"
os.environ["DB_TABLE_PREFIX"] = ""
os.environ["RBA_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["RBA_REPORTS_DIR"] = str(_TEST_DATA_DIR / "reports")
os.environ["RBA_EVIDENCE_DIR"] = str(_TEST_DATA_DIR / "evidence")

from tests.fixtures.server import FixtureServer  # noqa: E402


@pytest.fixture()
def origin() -> FixtureServer:
    server = FixtureServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def data_dir() -> Path:
    return _TEST_DATA_DIR
