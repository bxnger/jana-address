"""
Известные проблемные случаи и неоднозначные адреса.
Ожидания — по правилам регламента. Где значение из строки надёжно не выводится,
тест проверяет, что парсер НЕ выдумывает его и снижает confidence с причиной.
"""
import pytest

from address_parser import compute_confidence, explain_address, parse_address
from evaluate import norm_value

F = lambda raw: parse_address(raw, "t")  # noqa: E731


def check(raw, **expected):
    r = F(raw)
    bad = {k: (v, r[k]) for k, v in expected.items()
           if norm_value(r[k], strip_slash=False) != norm_value(v, strip_slash=False)}
    assert not bad, f"{raw!r}: {bad}"
    return r


# --- казахский / русский -------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Алматы қаласы, Абай даңғылы 10", dict(city="Алматы", province="Алматы", street_name="Абай", house="10")),
    ("Шымкент/Шымкент қаласы, Тәуке хан даңғылы 5", dict(city="Шымкент", street_name="Тәуке хан", house="5")),
    ("Ақмола облысы, Көкшетау қ., Абай көшесі 5", dict(province="Акмолинская область", city="Кокшетау", street_name="Абай")),
    ("город Актобе/Ақтөбе қаласы, Абая 1", dict(city="Актобе", province="Актюбинская область")),
    ("Ақтөбе қаласы/город Актобе, Абая 1", dict(city="Актобе")),
])
def test_kazakh_russian(raw, exp):
    check(raw, **exp)


# --- область против города --------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Актюбинская область, г. Актобе, Абая 3", dict(province="Актюбинская область", city="Актобе")),
    ("Актобе обл, Хромтау, Абая 3", dict(province="Актюбинская область", city="Хромтау")),
    ("Карагандинская обл., Караганда, Бухар жырау 10", dict(province="Карагандинская область", city="Караганда")),
])
def test_province_vs_city(raw, exp):
    r = check(raw, **exp)
    assert r["city"] != r["province"]


def test_province_city_conflict_kept_as_written_and_flagged():
    x = explain_address("Алматинская область, Алматы, Абая 5")
    assert x["result"]["province"] == "Алматинская область"   # не переписываем строку справочником
    assert "province_city_conflict" in x["flags"]
    assert x["result"]["confidence"] < 0.9


# --- микрорайоны -----------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("мкр 4, 12, кв 7", dict(street_type="микрорайон", street_name="4", house="12", apartment="7")),
    ("4 мкр 12 кв 7", dict(street_name="4", house="12", apartment="7")),
    ("Самал 2 мкр, 15", dict(street_name="Самал 2", house="15")),
    ("мкр. Самал-1 д. 5", dict(street_name="Самал-1", house="5")),
    ("Нурсити 2 микрорайон 11дом 5 подьезд 239 кв", dict(street_name="Нурсити 2", house="11", entrance="5", apartment="239")),
])
def test_microdistrict_numbers(raw, exp):
    check(raw, **exp)


# --- 8 Марта, 12а, 15-3, слитные -----------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("8 Марта 12", dict(street_name="8 Марта", house="12")),
    ("ул 8 марта 12а", dict(street_name="8 марта", house="12а")),
    ("Абая 12 А кв 3", dict(house="12А", apartment="3")),
    ("Абая 12a", dict(house="12а")),                 # латинская a в кириллической строке
    ("Абая 15-3", dict(house="15", apartment="3")),   # трактовка команды, см. README
    ("Абая15кв3", dict(street_name="Абая", house="15", apartment="3")),
    ("д12кв3 Абая", dict(street_name="Абая", house="12", apartment="3")),
    ("д.12кв.5", dict(house="12", apartment="5")),
    ("12дом 69квартира", dict(house="12", apartment="69")),
    ("ул. Абая 1/2", dict(house="1/2")),
])
def test_numbers(raw, exp):
    check(raw, **exp)


def test_fraction_not_equal_whole():
    assert F("ул. Абая 1/2")["house"] != "1"


# --- подъезд / этаж -------------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Абая 5 2под 3эт", dict(entrance="2", floor="3", apartment=None)),
    ("Абая 5, 2-й подъезд, 3-й этаж", dict(entrance="2", floor="3")),
    ("Абая 5 подъезд2 этаж3", dict(entrance="2", floor="3")),
    ("Абая 15, 4 подъезд 9 этаж", dict(entrance="4", floor="9")),
    ("Абая 15, 9 этаж 4 подъезд", dict(entrance="4", floor="9")),
    ("Абая 15 3 подъезд 5 этаж", dict(house="15", entrance="3", floor="5")),
])
def test_entrance_floor(raw, exp):
    check(raw, **exp)


def test_pod_preposition_not_entrance():
    r = F("Под мостом 5")
    assert r["entrance"] is None


# --- кв-л --------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("кв-л 6, д 12, кв 4", dict(street_type="квартал", street_name="6", house="12", apartment="4")),
    ("66 кв-л 12 дом", dict(street_type="квартал", street_name="66", house="12", apartment=None)),
    ("6 квартал 12/1", dict(street_type="квартал", street_name="6", house="12/1", apartment=None)),
])
def test_kvartal(raw, exp):
    check(raw, **exp)


# --- корпуса и блоки: поля нет -> не выдумываем квартиру ------------------------------
@pytest.mark.parametrize("raw,apt", [
    ("Абая 150 корп. 2", None),
    ("Абая 150 корпус 2 кв 15", "15"),
    ("Абая 150 к2 кв 15", "15"),
    ("Абая 150 блок Б кв 3", "3"),
    ("Абая 150, блок 3, кв 12", "12"),
    ("Абая 5 стр. 1", None),
])
def test_building_part_not_apartment(raw, apt):
    x = explain_address(raw)
    assert x["result"]["house"] == "150" or x["result"]["house"] == "5"
    assert x["result"]["apartment"] == apt
    assert "building_part" in x["flags"]


def test_house_letter_k_not_building():
    assert F("Карасай батыра 5к")["house"] == "5к"


# --- латиница внутри кириллических слов -------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Aбaя 15", dict(street_name="Абая", house="15")),
    ("ул. Сaтпaевa 5", dict(street_name="Сатпаева")),
    ("Almaty, Abaya 15 kv 3", dict(city="Алматы", street_name="Abaya", apartment="3")),  # чистая латиница — не трогаем
])
def test_latin_homoglyphs(raw, exp):
    check(raw, **exp)


# --- пустые и длинные -------------------------------------------------------------------
def test_empty_and_whitespace():
    for raw in ["", " ", "\t\n", "​﻿"]:
        r = F(raw)
        assert r["raw_address"] == raw and r["confidence"] == 0.0
        assert all(r[k] is None for k in ("city", "street_name", "house", "apartment"))


def test_long_whitespace_prefix_not_lost():
    r = F(" " * 600 + "Абая 5")
    assert r["street_name"] == "Абая" and r["house"] == "5"


def test_long_garbage_truncated_and_flagged():
    raw = "Абая 5 " + "x" * 600
    x = explain_address(raw)
    assert x["result"]["house"] == "5" and "truncated" in x["flags"]
    assert x["result"]["confidence"] < 0.6
    assert F(raw)["raw_address"] == raw


# --- неоднозначные: не выдумываем, снижаем confidence -----------------------------------
def test_extra_number_not_silently_dropped():
    x = explain_address("Абая 15, 3, кв 5")
    assert x["result"]["apartment"] == "5" and "3" in x["leftover"]
    assert x["result"]["confidence"] < 0.9


@pytest.mark.parametrize("raw", ["15 Абая", "кв 5", "5", "Абая"])
def test_incomplete_low_confidence(raw):
    assert F(raw)["confidence"] <= 0.6


def test_alone_city_name_with_number_is_street_not_city():
    r = F("Алматы 15")          # "улица Алматы 15" так же вероятна, как город
    assert r["city"] is None


@pytest.mark.parametrize("raw", ["Аксай, 12 дом", "Абай, Абая 5", "Шу, Абая 5"])
def test_ambiguous_city_province_is_flagged_guess(raw):
    # область заполняется (пустая по метрике никогда не верна), но помечается как догадка
    x = explain_address(raw)
    if x["result"]["province"] is not None:
        assert "province_from_ambiguous_city" in x["flags"]
        assert x["result"]["confidence"] <= 0.85


def test_unknown_first_segment_city_is_flagged():
    # Самал — мкр. Без внешнего справочника неотличимо от города -> флаг city_by_position;
    # с OSM-справочником (основа "Самал-1/2") -> living_area. Область не выдумывается в обоих случаях.
    x = explain_address("Самал, Абая 15")
    r = x["result"]
    assert ("city_by_position" in x["flags"]) or (r["living_area"] == "Самал" and r["city"] is None)
    assert r["province"] is None


# --- confidence: эвристика с объяснимыми причинами ---------------------------------------
def test_confidence_documented_as_not_probability():
    assert "НЕ откалиброванная вероятность" in compute_confidence.__doc__


@pytest.mark.parametrize("raw", [
    "Абая 15-3", "Абая 150 корп. 2", "Самал, Абая 15", "Алматинская область, Алматы, Абая 5",
    "Абая 15, 3, кв 5", "Абая 5 " + "x" * 600, "Абай, Абая 5",
])
def test_low_confidence_has_reason(raw):
    x = explain_address(raw)
    assert x["result"]["confidence"] < 1.0
    assert x["flags"] or x["leftover"], "уверенность снижена, но причина не видна"


def test_confidence_range_and_monotonic_on_garbage():
    good = F("г. Алматы, ул. Абая 15, кв 3")["confidence"]
    junk = F("Абая 15 бла бла бла бла")["confidence"]
    assert 0.0 <= junk < good <= 1.0
