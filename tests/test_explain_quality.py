"""Объяснимость, кандидаты из справочника, анализ качества."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import process_record  # noqa: E402
from app.quality import compare, norm_value  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def run(raw):
    return process_record({"id": 1, "raw_address": raw}, debug=True)


def test_every_field_is_explained():
    r = run("Туран 55/6 магазин береке 4подьезд 9этаж")
    ex = r["_explain"]
    assert set(ex) == {"province", "city", "district", "living_area", "street_type", "street_name",
                       "house", "apartment", "entrance", "floor", "landmark"}
    assert ex["entrance"]["status"] == "ok" and "подьезд" in ex["entrance"]["text"]
    assert ex["floor"]["status"] == "ok" and "этаж" in ex["floor"]["text"]
    assert ex["landmark"]["status"] == "ok" and "магазин" in ex["landmark"]["text"]
    assert "дроб" in ex["house"]["text"]
    assert ex["city"]["status"] == "missing" and ex["city"]["text"]


def test_district_explains_how_it_was_restored():
    r = run("г. Алматы, ул. Жандосова 5, кв 12")
    assert r["district"] == "Бостандыкский"
    assert "по номеру дома" in r["_explain"]["district"]["text"]


def test_noise_is_explained():
    r = run("г. Алматы, ул. Жандосова 5, кв 12, 87071234567")
    assert any("Телефон" in n for n in r["_notes"])


@pytest.mark.parametrize("raw,field,expected", [
    ("г. Алматы, ул. Абаяя 10, кв 5", "street_name", "Абая"),
    ("Алматыы, Абая 10", "city", "Алматы"),
    ("Шымкнт, Тауке хана 12", "city", "Шымкент"),
])
def test_suggestions_offer_but_never_replace(raw, field, expected):
    r = run(raw)
    assert expected in [c["value"] for c in r["_suggestions"][field]]
    assert r[field] != expected                       # в ответе — как написано, без подмены
    assert field in r["_doubt"]                       # поле подсвечено как сомнительное


def test_known_street_has_no_suggestions():
    assert "street_name" not in run("г. Алматы, ул. Жандосова 5")["_suggestions"]


def test_doubt_is_field_level_with_reason():
    r = run("Алматы, ул. Абая, Жандосова, 5, кв 12")
    assert "street_name" in r["_doubt"] and "apartment" not in r["_doubt"]
    assert "угол" in r["_explain"]["street_name"]["text"]


def test_kato_second_source():
    from app.gazetteer import get_gazetteer
    gz = get_gazetteer()
    assert gz.in_kato("Алматы", "Бостандыкский") == {"city": True, "district": True}
    assert gz.in_kato("Несуществующийград") == {"city": False}
    r = run("г. Алматы, ул. Жандосова 5, кв 12")
    assert "КАТО" in r["_explain"]["city"]["text"] and "КАТО" in r["_explain"]["district"]["text"]
    # Байконыр есть в КАТО, но не в OSM — теперь узнаётся как город
    assert run("Байконыр, ул. Абая 5")["city"]


def test_compare_by_jury_rules():
    gold = [{"id": 1, "raw_address": "x", "city": "Алматы", "street_name": "Абая", "house": "1/2", "apartment": "5"},
            {"id": 2, "raw_address": "y", "city": "Астана", "street_name": "Кенесары", "house": "3"}]
    preds = [{"id": 2, "city": "астана", "street_name": "Кенесары.", "house": "3"},
             {"id": 1, "city": "Алматы", "street_name": "Абая", "house": "12", "apartment": None}]
    rep = compare(preds, gold)
    assert rep["matched_by"] == "id"
    assert rep["fields"]["city"]["accuracy"] == 1.0           # регистр и пунктуация не важны
    assert rep["fields"]["house"]["wrong"] == 1                # «1/2» и «12» — разные значения
    assert rep["records_with_errors"] == 1                     # квартира не в метрике
    assert rep["examples"]["house"][0]["expected"] == "1/2"
    assert norm_value(" Абая, 10 ") == "абая 10"


def test_evaluate_endpoint_on_benchmark():
    from fastapi.testclient import TestClient

    from app.api import app
    gold = json.loads((ROOT / "data" / "demo" / "benchmark_gold.json").read_text(encoding="utf-8"))[:200]
    with TestClient(app) as c:
        assert "internet" in c.get("/health").json()
        rep = c.post("/api/v1/evaluate", json=gold).json()
    assert rep["records"] == 200 and rep["score"] > 0.95
    assert rep["summary"]["seconds_per_1000"] < 60
    assert set(rep["groups"]) == {"address", "details", "reference"}
