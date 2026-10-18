"""API не падает ни на каком входе: 200 с ответом на каждую запись или понятная 4xx, но никогда 500."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import app  # noqa: E402

FIELDS = ["id", "raw_address", "province", "city", "district", "living_area", "street_type", "street_name",
          "house", "apartment", "entrance", "floor", "landmark", "confidence"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


WEIRD_RECORDS = [
    {"id": 1, "raw_address": None},
    {"id": 2, "raw_address": ""},
    {"id": 3, "raw_address": "   "},
    {"id": 4, "raw_address": 12345},
    {"id": 5, "raw_address": ["список", "строк"]},
    {"id": 6, "raw_address": {"вложенный": "объект"}},
    {"id": 7},
    {"raw_address": "без id"},
    {"id": 9, "raw_address": "🏠🚚 ул. Абая 10 ✨"},
    {"id": 10, "raw_address": "\x00\x01\x07 ул. Абая 10 ​﻿"},
    {"id": 11, "raw_address": "кв" * 500},
    {"id": 12, "raw_address": "1/2/3/4/5/6/7/8/9 " * 50},
    {"id": 13, "raw_address": "Абая " + "1" * 400},
    {"id": 14, "raw_address": ",,,,,;;;;;////" * 50},
    {"id": {"сложный": "id"}, "raw_address": "ул. Абая 10"},
    "просто строка вместо записи",
    42,
]


def test_weird_records_answered_in_order(client):
    r = client.post("/api/v1/parse/batch", json=WEIRD_RECORDS)
    assert r.status_code == 200
    out = r.json()
    assert len(out) == len(WEIRD_RECORDS)
    for rec in out:
        assert list(rec) == FIELDS
        assert 0.0 <= rec["confidence"] <= 1.0


@pytest.mark.parametrize("body", ['"строка"', "42", "null", "{}", '{"a": 1}', "[", "не json", ""])
def test_bad_body_is_4xx_not_500(client, body):
    r = client.post("/api/v1/parse/batch", content=body.encode(), headers={"Content-Type": "application/json"})
    assert 400 <= r.status_code < 500


def test_empty_list_ok(client):
    r = client.post("/api/v1/parse/batch", json=[])
    assert r.status_code == 200 and r.json() == []


def test_huge_string_is_fast(client):
    raw = "г. Алматы, ул. Абая " + "очень длинный адрес с повторами 12/3 кв 5 " * 2500   # ≈100 тыс. символов
    t = time.time()
    r = client.post("/api/v1/parse/batch", json=[{"id": 1, "raw_address": raw}])
    assert r.status_code == 200 and time.time() - t < 10


def test_big_batch_keeps_ids(client):
    recs = [{"id": f"r{i}", "raw_address": f"ул. Абая {i}, кв {i % 90 + 1}"} for i in range(5000)]
    t = time.time()
    out = client.post("/api/v1/parse/batch", json=recs).json()
    assert [o["id"] for o in out] == [r["id"] for r in recs]
    assert time.time() - t < 30


def test_bad_file_upload_is_400(client):
    r = client.post("/api/v1/parse/file", files={"file": ("x.json", b"\xff\xfe not json", "application/json")})
    assert r.status_code == 400
