"""Сложные адреса (data/demo/hard_cases.py): казахский, старые названия, КАПС, слитное написание,
разделители-точки, сёла, шум. Проверяются только поля, перечисленные в ожидании."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "demo"))

from hard_cases import CASES  # noqa: E402

from app.pipeline import process_record  # noqa: E402


@pytest.mark.parametrize("raw,expected", CASES, ids=[f"{i:02d}" for i in range(len(CASES))])
def test_hard_case(raw, expected):
    got = process_record({"id": 1, "raw_address": raw})
    diff = {k: (got.get(k), v) for k, v in expected.items() if (got.get(k) or None) != v}
    assert not diff, f"{raw!r}: (получено, ожидалось) {diff}"
