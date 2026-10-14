import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTDATA = ROOT / "testdata"


def load_testdata(name: str):
    return json.loads((TESTDATA / name).read_text(encoding="utf-8"))


@pytest.fixture
def testdata():
    return load_testdata
