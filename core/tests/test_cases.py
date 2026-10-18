"""
Регрессия по размеченным наборам tests/cases.json, holdout.json (использован в разработке) и live.json
(черновик живого набора, разметка не подтверждена организаторами)
и astana_samples.json (30 синтетических адресов Астаны, не реальные клиенты).
Сравнение — по правилам жюри (нижний регистр, без пунктуации, лишних пробелов),
той же функцией, что и в evaluate.py.
"""
import json
from pathlib import Path

import pytest

import gazetteer as gz
from address_parser import parse_address
from evaluate import norm_value

_HERE = Path(__file__).parent
CASES = [c for name in ("cases.json", "holdout.json", "live.json", "astana_samples.json")
         for c in json.loads((_HERE / name).read_text(encoding="utf-8"))["cases"]]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case):
    if "needs_osm" in case.get("tags", []) and not any(gz.MULTIWORD_STREETS.values()):
        pytest.skip("кейс опирается на OSM-справочник улиц (data/core_gazetteer.json)")
    r = parse_address(case["raw"], case["id"])
    assert r["raw_address"] == case["raw"]
    problems = []
    for field, expected in case.get("expected", {}).items():
        # строже регламента: "/" сохраняем, чтобы "1/2" не совпадало с "12"
        if norm_value(r[field], strip_slash=False) != norm_value(expected, strip_slash=False):
            problems.append(f"{field}: ожидали {expected!r}, получили {r[field]!r}")
    for field in case.get("expect_null", []):
        if r[field] is not None:
            problems.append(f"{field}: ожидали null, получили {r[field]!r}")
    assert not problems, f"{case['raw']!r}\n  " + "\n  ".join(problems)
