"""баги, найденные на перегенерированном seed 123 и слепых seed 5555/9001/9002,
и на живом наборе (tests/live.json). Часть кейсов нужна OSM-справочник (пропускаются без него)."""
from pathlib import Path

import pytest

import gazetteer as gz
from address_parser import explain_address, parse_address
from evaluate import norm_value

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


def check(raw, **exp):
    r = parse_address(raw, "t")
    bad = {k: (v, r[k]) for k, v in exp.items()
           if norm_value(r[k], strip_slash=False) != norm_value(v, strip_slash=False)}
    assert not bad, f"{raw!r}: {bad}"
    return r


# --- слово-маркер ориентира после типа — это название ("мкр Школа-интернат") ---------------------
@pytest.mark.parametrize("raw,exp", [
    ("Акколь, микрорайон Школа-интернат, 36, квартира 166",
     dict(street_type="микрорайон", street_name="Школа-интернат", house="36", apartment="166", landmark=None)),
    ("мкр Школа 5", dict(street_name="Школа", house="5")),
])
def test_landmark_word_after_type_is_name(raw, exp):
    check(raw, **exp)


@pytest.mark.parametrize("raw,lm", [
    ("Абая 15, рядом со школой", "рядом со школой"),
    ("мкр Самал, напротив школы, 5", "напротив школы"),
    ("Аксай Строитель 4 микрорайон 12гдом напротив школы", "напротив школы"),
])
def test_landmark_preposition_still_landmark(raw, lm):
    assert norm_value(parse_address(raw)["landmark"]) == norm_value(lm)


# --- номер мкр отдельным сегментом, дом найден по ключевому слову --------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("г.Алматы,  микрорайон Коктем 2, ул.Байрона дом 49 kv 253", dict(living_area="Коктем 2", street_name="Байрона", house="49", apartment="253")),
    ("г.Зачаганск, мкрн ПДП 2, ул.бокен би 177дом, квартира 122", dict(living_area="ПДП 2", street_name="бокен би", house="177")),
    ("город Актобе, мкрн 12ВГ, ул. Сержана 47дом", dict(living_area="12ВГ", street_name="Сержана", house="47")),
    ("микрорайон разъезд 103, ул.Шмидта, 4дом, 245кв", dict(living_area="разъезд 103", street_name="Шмидта", house="4", apartment="245")),
    ("г. Аксай, Аксай, Мкр Строитель 4, Ул .Джамбульская, дом 3", dict(living_area="Строитель 4", street_name="Джамбульская", house="3")),
])
def test_mkr_number_kept_in_living_area(raw, exp):
    check(raw, **exp)


def test_untyped_mkr_with_number_and_keyword_house():
    # "Самал 2 дом 58": номер относится к названию мкр, а не пропадает в остатке
    r = check("алматы самал 2 дом 58 кв 12", street_name="самал 2", house="58", apartment="12")
    assert r["city"].lower() == "алматы"


def test_house_letter_number_not_absorbed_when_unknown_house():
    x = explain_address("Абая 15, 3, кв 5")            # лишнее число не пропадает молча
    assert x["result"]["house"] == "15" and "3" in x["leftover"]


# --- «Набережная» как название ----------------------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("город Петропавловск, мкрн Хромзавод, Набережная 17 кв 5", dict(living_area="Хромзавод", street_name="Набережная", house="17")),
    ("г. Тобыл, мкр Нурай, Набережная д.8", dict(living_area="Нурай", street_name="Набережная", house="8")),
])
def test_naberezhnaya_name_with_mkr(raw, exp):
    check(raw, **exp)


# --- город после адреса -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Мамыр-4 дом 101 кв 22 Алматы", dict(city="Алматы", street_name="Мамыр-4", house="101", apartment="22")),
    ("Абая 15 Алматы", dict(city="Алматы", street_name="Абая", house="15")),
])
def test_city_last(raw, exp):
    check(raw, **exp)


@pytest.mark.parametrize("raw", ["ул. Алматы", "улица Алматы 5", "мкр Алматы 3, дом 5"])
def test_city_name_after_type_is_not_city(raw):
    assert parse_address(raw)["city"] is None


# --- город без запятой (нужен OSM) ----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Аксай Строитель 4 микрорайон 12гдом напротив школы", dict(city="Аксай", street_name="Строитель 4", house="12г")),
    ("Байконур Набережная д. 199, 6 подъезд", dict(city="Байконур", street_name="Набережная", house="199", entrance="6")),
])
def test_city_without_comma_round4(osm, raw, exp):
    check(raw, **exp)
