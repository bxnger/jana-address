"""Регрессия: сокращения районов, «левый берег», район без слова «район»."""
from pathlib import Path

import pytest

import gazetteer as gz
from address_parser import parse_address

REAL = Path(__file__).resolve().parents[2] / "data" / "core_gazetteer.json"


@pytest.fixture
def osm():
    if not REAL.is_file():
        pytest.skip("нет data/core_gazetteer.json")
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    assert gz.load_external(REAL)
    yield
    gz._restore(snap)
    gz.EXTERNAL_ERROR = err

CASES = [
    ("алматы наурызбайский район шанырак 2 ул жанкожа батыра 202",
     {"living_area": "шанырак 2", "street_name": "жанкожа батыра", "house": "202", "apartment": None}),
    ("Астана р-н Байқоңыр ул Ыкылас Дукенулы 35 кв 7",
     {"city": "Астана", "district": "Байконырский", "house": "35"}),
    ("Астана, Сарыарка, Бейбітшілік көшесі 10, 25 пәтер",
     {"district": "Сарыарка", "street_name": "Бейбітшілік", "house": "10"}),
    ("алматы бостандык аль-фараби 77/7 кв 200",
     {"district": "Бостандыкский", "street_name": "аль-фараби", "house": "77/7"}),
    ("Астана левый берег Кабанбай батыра 11 жк Хайвилл блок В кв 120",
     {"street_name": "Кабанбай батыра", "house": "11"}),
]


@pytest.mark.parametrize("raw,exp", CASES)
def test_regressions(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)
