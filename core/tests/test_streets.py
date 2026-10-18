"""ключ "streets" справочника — загрузка, поиск, применение в парсере, confidence."""
import json
import os
from pathlib import Path

import pytest

import gazetteer as gz
from address_parser import explain_address, parse_address

MINI = {
    "cities": {"Новоград": "Акмолинская область"},
    "living_areas": ["Самал-1", "Самал 2", "Самал микрорайон", "Аксай 3А"],
    "streets": {
        "Алматы": {"Абая": "проспект", "Жандосова": "улица", "Рыбинский": "переулок",
                   "Нурсултана Назарбаева": "проспект", "Достык": None},
        "Новоград": {"Самал": "улица", "Ленина": "улица"},
    },
}


@pytest.fixture
def mini(tmp_path):
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    for n in ("STREETS", "_STREET_LAST_WORD", "LIVING_AREA_BASES", "KNOWN_LIVING_AREAS"):
        getattr(gz, n).clear()          # изоляция от подключённого OSM-справочника
    f = tmp_path / "g.json"
    f.write_text(json.dumps(MINI, ensure_ascii=False), encoding="utf-8")
    assert gz.load_external(f) is True
    yield
    gz._restore(snap)
    gz.EXTERNAL_ERROR = err


# --- справочник -----------------------------------------------------------------------
def test_lookup_functions(mini):
    assert gz.is_known_street("Абая", "Алматы")
    assert gz.is_known_street("абая", "г. Алматы".split()[-1])
    assert gz.street_type_for("Абая", "Алматы") == "проспект"
    assert gz.street_type_for("Достык", "Алматы") is None          # тип null в справочнике
    assert gz.is_known_street("Назарбаева", "Алматы")               # по последнему слову
    assert not gz.is_known_street("Назарбаева", "Алматы", exact=True)
    assert gz.street_type_for("Назарбаева", "Алматы") is None       # тип — только при точном совпадении
    assert not gz.is_known_street("Абая", None)                     # без города не угадываем
    assert not gz.is_known_street("Абая", "Неизвестноград")
    assert gz.is_living_area_base("Самал") and not gz.is_living_area_base("Абая")


def test_no_streets_without_external_file():
    if gz.EXTERNAL_LOADED:
        pytest.skip("подключён внешний справочник")
    assert not gz.has_streets("Алматы") and not gz.is_known_street("Абая", "Алматы")


@pytest.mark.parametrize("bad", [{"streets": [1]}, {"streets": {"Алматы": ["Абая"]}},
                                 {"streets": {"Алматы": {"Абая": 5}}}])
def test_broken_streets_rolls_back(tmp_path, bad):
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    try:
        f = tmp_path / "g.json"
        f.write_text(json.dumps({"cities": {"Полуград": "Акмолинская область"}, **bad}), encoding="utf-8")
        assert gz.load_external(f) is False and gz.EXTERNAL_ERROR
        assert not gz.is_known_city("Полуград")        # частично загруженное откатывается
    finally:
        gz._restore(snap)
        gz.EXTERNAL_ERROR = err


# --- «Самал, Абая 15» --------------------------------------------------------------------
def test_samal_is_living_area_not_city(mini):
    x = explain_address("Самал, Абая 15")
    r = x["result"]
    assert r["city"] is None and r["living_area"] == "Самал" and r["province"] is None
    assert r["street_name"] == "Абая" and r["house"] == "15"
    assert "living_area_from_gazetteer" in x["flags"]


def test_samal_with_city(mini):
    r = parse_address("Алматы, Самал, Абая 15")
    assert (r["city"], r["living_area"], r["street_name"], r["house"]) == ("Алматы", "Самал", "Абая", "15")


def test_samal_is_street_in_city_where_it_is_a_street(mini):
    r = parse_address("Новоград, Самал 5")
    assert r["street_name"] == "Самал" and r["living_area"] is None


# --- сегмент-улица в начале строки --------------------------------------------------------
def test_street_first_then_city_swapped(mini):
    x = explain_address("Абая, Алматы 15")
    r = x["result"]
    assert r["city"] == "Алматы" and r["street_name"] == "Абая" and r["house"] == "15"
    assert "city_street_swapped" in x["flags"]


def test_no_swap_without_gazetteer_evidence(mini):
    r = parse_address("Неизвестная, Алматы 15")   # "Неизвестная" не улица Алматы -> не переставляем
    assert r["street_name"] == "Алматы"


# --- тип улицы из справочника ---------------------------------------------------------------
def test_street_type_from_gazetteer(mini):
    x = explain_address("Алматы, Жандосова 58")
    assert x["result"]["street_type"] == "улица" and x["result"]["street_name"] == "Жандосова"
    assert "street_type_from_gazetteer" in x["flags"]


@pytest.mark.parametrize("raw", ["Жандосова 58",                       # город неизвестен
                                 "Алмалинский р-н, Абая 15",            # город восстановлен по району
                                 "Аксай, Абая 5"])                      # неоднозначный город
def test_street_type_not_filled_without_reliable_city(mini, raw):
    x = explain_address(raw)
    assert x["result"]["street_type"] is None and "street_type_from_gazetteer" not in x["flags"]


def test_written_type_not_overwritten(mini):
    r = parse_address("г. Алматы, ул. Абая 15")       # в справочнике "проспект", в строке "улица"
    assert r["street_type"] == "улица"


def test_street_name_never_corrected(mini):
    r = parse_address("Алматы, Назарбаева 100")
    assert r["street_name"] == "Назарбаева"            # не "Нурсултана Назарбаева"
    assert r["street_type"] is None                    # тип — только при точном совпадении


def test_area_types_not_checked(mini):
    x = explain_address("Алматы, мкр 4, 12")
    assert "street_not_in_gazetteer" not in x["flags"] and x["result"]["street_type"] == "микрорайон"


# --- confidence -----------------------------------------------------------------------------
def test_confidence_known_vs_unknown_street(mini):
    known = explain_address("г. Алматы, ул. Абая 15")
    unknown = explain_address("г. Алматы, ул. Абаяя 15")
    assert "street_in_gazetteer" in known["flags"]
    assert "street_not_in_gazetteer" in unknown["flags"]
    assert unknown["result"]["confidence"] < known["result"]["confidence"]


def test_no_gazetteer_flags_for_city_without_streets(mini):
    x = explain_address("г. Караганда, ул. Абая 15")   # в mini нет улиц Караганды
    assert not {"street_in_gazetteer", "street_not_in_gazetteer"} & set(x["flags"])


# --- реальный OSM-справочник (если лежит рядом) ---------------------------------
REAL = Path(__file__).resolve().parents[2] / "data" / "core_gazetteer.json"


@pytest.mark.skipif(not REAL.is_file(), reason="data/core_gazetteer.json не найден")
def test_real_osm_gazetteer(tmp_path):
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    try:
        assert gz.load_external(REAL) is True, gz.EXTERNAL_ERROR
        assert gz.street_type_for("Абая", "Алматы") == "проспект"
        r = parse_address("Самал, Абая 15")
        assert r["city"] is None and r["living_area"] == "Самал"
        r = parse_address("Алматы, Рыбинский 5")
        assert r["street_type"] == "переулок" and r["street_name"] == "Рыбинский"
    finally:
        gz._restore(snap)
        gz.EXTERNAL_ERROR = err
