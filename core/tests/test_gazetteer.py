"""Справочник: отсутствующий/пустой/битый файл, неизвестные и неоднозначные названия."""
import json

import pytest

import gazetteer as gz
from address_parser import explain_address, parse_address


@pytest.fixture
def restore_gz():
    snap = gz._snapshot()
    err = gz.EXTERNAL_ERROR
    yield
    gz._restore(snap)
    gz.EXTERNAL_ERROR = err


@pytest.mark.parametrize("content", ["", "   ", "{bad json", "[1, 2]", "null", '"text"',
                                     '{"cities": [1, 2]}', '{"living_areas": 5}'])
def test_broken_file_does_not_crash(tmp_path, restore_gz, content):
    f = tmp_path / "g.json"
    f.write_text(content, encoding="utf-8")
    assert gz.load_external(f) is False
    assert gz.EXTERNAL_ERROR                       # причина доступна
    assert parse_address("г. Алматы, Абая 5")["city"] == "Алматы"   # встроенный минимум работает


def test_missing_file_returns_false_without_error(tmp_path, restore_gz):
    assert gz.load_external(tmp_path / "nope.json") is False
    assert gz.EXTERNAL_ERROR is None


def test_empty_object_loads(tmp_path, restore_gz):
    f = tmp_path / "g.json"
    f.write_text("{}", encoding="utf-8")
    assert gz.load_external(f) is True


def test_external_data_is_used(tmp_path, restore_gz):
    f = tmp_path / "g.json"
    f.write_text(json.dumps({"cities": {"Новоград": "Акмолинская область"},
                             "living_areas": ["Самал"]}, ensure_ascii=False), encoding="utf-8")
    assert gz.load_external(f) is True
    r = parse_address("Новоград, Абая 5")
    assert r["city"] == "Новоград" and r["province"] == "Акмолинская область"
    r = parse_address("Алматы, Самал, Абая 5")
    assert r["living_area"] == "Самал" and r["street_name"] == "Абая"


def test_unknown_names_give_no_invented_values():
    assert gz.province_for_city("Неизвестноград") is None
    assert gz.province_from_text("Неизвестная") is None
    assert gz.province_for_district("Неизвестный") is None
    r = parse_address("Неизвестноград, Абая 5")
    assert r["province"] is None                    # область не выдумываем


def test_ambiguous_district_returns_none():
    assert gz.province_for_district("Жамбылский") is None   # есть в нескольких областях


@pytest.mark.parametrize("raw", ["Аксай, Абая 5", "Абай, Абая 5", "Шу, Абая 5"])
def test_ambiguous_city_without_marker_province_flagged(raw):
    x = explain_address(raw)
    assert x["result"]["province"] is not None and "province_from_ambiguous_city" in x["flags"]


def test_ambiguous_city_with_marker_ok():
    r = parse_address("г. Аксай, ул. Ленина 5")
    assert r["city"] == "Аксай" and r["province"] == "Западно-Казахстанская область"


def test_ambiguous_city_reason_in_explain():
    assert "province_from_ambiguous_city" in explain_address("Абай, Абая 5")["flags"]
