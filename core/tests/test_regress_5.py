"""запятая внутри названия улицы ('ул. Керей, Жанибек хандар, 18').
Склейка принимается только если такое название есть в справочнике улиц (в любом городе);
иначе имя не меняем, а запись помечаем и снижаем confidence."""
import pytest

import gazetteer as gz
from address_parser import explain_address, parse_address


@pytest.fixture
def mini():
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    gz._add_streets("Астана", {"Керей и Жанибек хандар": "улица", "Абая": "проспект"})
    gz._add_streets("Алматы", {"Керей-Жанибек хандар": "улица"})
    yield
    gz._restore(snap)
    gz.EXTERNAL_ERROR = err


def test_comma_in_street_name_merged(mini):
    for raw in ("ул. Керей, Жанибек хандар, 18", "Астана, ул. Керей, Жанибек хандар, 18, кв 5",
                "Алматы, улица Керей, Жанибек хандар 18"):
        x = explain_address(raw)
        r = x["result"]
        assert r["street_name"] == "Керей, Жанибек хандар", raw
        assert r["house"] == "18" and not x["leftover"], raw
        assert "street_comma_in_name" in x["flags"]


def test_merge_does_not_break_known_city_in_comma(mini):
    r = parse_address("ул. Абая, Алматы, 15", "t")
    assert (r["street_name"], r["city"], r["house"]) == ("Абая", "Алматы", "15")


def test_unknown_comma_segment_not_merged_but_penalised(mini):
    x = explain_address("ул. Абая, Жандосова, 5")
    r = x["result"]
    assert r["street_name"] == "Абая" and r["house"] == "5"
    assert "street_comma_split" in x["flags"] and r["confidence"] < 0.7


def test_landmark_and_district_segments_untouched(mini):
    r = parse_address("ул. Абая, возле школы, 15", "t")
    assert r["street_name"] == "Абая" and r["landmark"] == "возле школы"
    r = parse_address("ул. Абая, Бостандыкский район, 15", "t")
    assert r["street_name"] == "Абая" and r["district"] == "Бостандыкский"


def test_known_street_accepts_comma_form(mini):
    assert gz.is_known_street("Керей, Жанибек хандар", "Астана")
    assert not gz.is_known_street("Керей, Жанибек хандар", "Шымкент")
