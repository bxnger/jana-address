"""баги, найденные синтетикой (scripts/gen_synthetic.py).
Строки — по образцу его примеров, ожидания — по правилам эталона. Часть кейсов зависит от
OSM-справочника (data/core_gazetteer.json) — они помечены и пропускаются без него."""
from pathlib import Path

import pytest

import gazetteer as gz
from address_parser import explain_address, parse_address
from evaluate import norm_value

REAL = Path(__file__).resolve().parents[2] / "data" / "core_gazetteer.json"


def check(raw, **exp):
    r = parse_address(raw, "t")
    bad = {k: (v, r[k]) for k, v in exp.items()
           if norm_value(r[k], strip_slash=False) != norm_value(v, strip_slash=False)}
    assert not bad, f"{raw!r}: {bad}"
    return r


@pytest.fixture
def osm():
    if not REAL.is_file():
        pytest.skip("нет data/core_gazetteer.json")
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    assert gz.load_external(REAL)
    yield
    gz._restore(snap)
    gz.EXTERNAL_ERROR = err


# --- кв-N (230 из 230 ошибок) -------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("ул.ермека серкебаева 49/1, кв-245", dict(street_name="ермека серкебаева", house="49/1", apartment="245")),
    ("улица Григория Потанина, д.163, кв-227 ТЦ Достык", dict(house="163", apartment="227")),
    ("ул Хариузовская, 8, 4подьезд, кв-142, 14 этаж", dict(house="8", apartment="142", entrance="4", floor="14")),
    ("микрорайон Карааул, 9/5 кв-254", dict(house="9/5", apartment="254")),
    ("переулок Тропинский 16 кв-61 подъезд 11", dict(house="16", apartment="61", entrance="11")),
    ("Самал-2, 15", dict(street_name="Самал-2", house="15")),            # дефис в названии — не квартира
])
def test_kv_dash(raw, exp):
    check(raw, **exp)


# --- мкр постфиксом + литера, слитная с "дом" -----------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Строитель 4 микрорайон 1бдом, кв-204, п-д 12", dict(street_name="Строитель 4", house="1б", apartment="204", entrance="12")),
    ("Самал-2 микрорайон 102бдом, кв. 46", dict(street_name="Самал-2", house="102б", apartment="46")),
    ("Абая 12дом", dict(house="12")),
])
def test_mkr_postfix_glued_letter(raw, exp):
    check(raw, **exp)


# --- п-д N --------------------------------------------------------------------------------
def test_pd_entrance():
    check("город Кокшетау, мкр Центральный, 8, п-д 4, кв 17", house="8", entrance="4", apartment="17")


# --- «ауыл» и слово-тип внутри названия -------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Мкр Алтын Ауыл, 109/4", dict(street_type="микрорайон", street_name="Алтын Ауыл", house="109/4")),
    ("Военный городок 1 микрорайон 75дом", dict(street_name="Военный городок 1", house="75")),
    ("Туркестан, Карабура аулие, 18б", dict(city="Туркестан", street_name="Карабура аулие", house="18б")),
    ("г. Алматы, ул. Набережная 26, kv 236", dict(street_name="Набережная", house="26", apartment="236")),
    ("г.Павлодар, улица набережная, дом 1/2", dict(street_name="набережная", house="1/2")),
    ("г. Актобе, Набережная168 kv 116", dict(street_name="Набережная", house="168", apartment="116")),
    ("город Деркул, мкр. садовое общество, Ул. Малик Габдуллин, 6дом",
     dict(living_area="садовое общество", street_name="Малик Габдуллин", house="6")),
    ("город Курмангазы, Егемен Казахстан, дом 11", dict(street_name="Егемен Казахстан", house="11")),
])
def test_type_words_inside_names(raw, exp):
    check(raw, **exp)


def test_country_segment_still_removed():
    r = check("Казахстан, г. Алматы, Абая 5", city="Алматы", street_name="Абая", house="5")
    assert "Казахстан" not in (r["street_name"] or "")


# --- инициалы: название как в строке ----------------------------------------------------------
@pytest.mark.parametrize("raw,name", [
    ("Туркестан, П.Ахмедов д.233б 6-этаж кв 30", "П.Ахмедов"),
    ("г.Туркестан, ул. А.Байтурсынова, д.117", "А.Байтурсынова"),
    ("город Туркестан, а.карабаев 3г, кв 199", "а.карабаев"),
    ("г. Туркестан, ул.с.отарбаев, д.200", "с.отарбаев"),
    ("Карабулак, ул. С. Рахимова, дом 47", "С. Рахимова"),
])
def test_initials_kept(raw, name):
    r = parse_address(raw)
    assert norm_value(r["street_name"]) == norm_value(name)
    assert r["city"] in ("Туркестан", "Карабулак")       # "С." после "ул." — не "село"


def test_village_marker_still_works():
    check("с. Акбулак, ул. Абая 5", city="Акбулак", street_name="Абая")
    check("г.Алматы ул.Абая 5", city="Алматы", street_name="Абая", house="5")


# --- мкр с "район" в названии ----------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("г.Риддер, мкрн 7-ой район, 17 кв.116", dict(street_name="7-ой район", district=None, house="17")),
    ("г. Кентау, Мкр 52 Район, 21 4этаж", dict(street_name="52 Район", house="21", floor="4")),
    ("мкр. 1-й район, 29г 11 подьезд 210квартира", dict(street_name="1-й район", house="29г", apartment="210")),
    ("город Кентау, мкрн Новастройка район, Муканова 30", dict(living_area="Новастройка район", street_name="Муканова")),
])
def test_mkr_with_raion(raw, exp):
    check(raw, **exp)


def test_real_district_still_district():
    check("Алмалинский р-н, Абая 15", district="Алмалинский", street_name="Абая")


# --- трёхзначный номер квартала/мкр ------------------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("город Уральск, квартал 112, 21, kv 295", dict(street_name="112", house="21", apartment="295")),
    ("мкр. разъезд 103, 6 кв. 57", dict(street_name="разъезд 103", house="6", apartment="57")),
])
def test_three_digit_area_number(raw, exp):
    check(raw, **exp)


# --- город без запятой (нужен OSM-справочник) ---------------------------------------------------
@pytest.mark.parametrize("raw,exp", [
    ("Алтай ул.Целинная дом 4", dict(city="Алтай", street_name="Целинная", house="4")),
    ("Мерке Жауғаш батыр көшесі дом 26", dict(city="Мерке", street_name="Жауғаш батыр", street_type="улица", house="26")),
    ("Жанаозен Отпан 4 квартира 238 2этаж", dict(city="Жанаозен", street_name="Отпан", house="4", apartment="238")),
    ("Мерке Н. Торгаева дом 26, кв-217", dict(city="Мерке", street_name="Н. Торгаева", apartment="217")),
    ("Индерборский Сарайшык18, кв-35, п-д 9", dict(city="Индерборский", street_name="Сарайшык", house="18")),
    ("Кентау 93 квартал, д. 39 223кв", dict(city="Кентау", street_name="93", house="39", apartment="223")),
    ("Акколь 90квартал 10дом, под. 8", dict(city="Акколь", street_name="90", house="10", entrance="8")),
    ("Аксай 14квартал 32дом kv 121", dict(city="Аксай", street_name="14", house="32")),
])
def test_city_without_comma(osm, raw, exp):
    check(raw, **exp)


@pytest.mark.parametrize("raw,exp", [
    ("Абай көшесі 15", dict(city=None, street_name="Абай", house="15")),   # "Абай" здесь улица
    ("Алматы 15", dict(city=None, street_name="Алматы")),
    ("Абая Сатпаева 15", dict(city=None)),
])
def test_city_without_comma_not_overeager(osm, raw, exp):
    check(raw, **exp)


# --- внешние алиасы не переименовывают реальные города ---------------------------------------------
@pytest.mark.parametrize("raw,city", [("г. Тобыл, Механизаторов 80", "Тобыл"), ("Арал, ул. Терешкова, 11б", "Арал")])
def test_external_alias_keeps_real_city(osm, raw, city):
    assert parse_address(raw)["city"] == city


# --- старые названия -> как в 2ГИС (ответ организаторов) ---------------------------------------------
@pytest.mark.parametrize("raw,city,province", [
    ("город Нур-Султан, Абая 5", "Астана", "Астана"),
    ("Нурсултан, Кенесары 40", "Астана", "Астана"),
    ("г. Семипалатинск, ул. Шакарима 10", "Семей", "Абайская область"),
    ("г. Капчагай, ул. Кунаева 3", "Конаев", "Алматинская область"),
    ("Алма-Ата, Абая 10", "Алматы", "Алматы"),
])
def test_old_city_names_as_in_2gis(raw, city, province):
    r = parse_address(raw)
    assert (r["city"], r["province"]) == (city, province)


def test_shevchenko_street_is_not_aktau():
    assert parse_address("ул. Шевченко 5, кв 2")["city"] is None


# --- неоднозначный город первым сегментом -----------------------------------------------------------
def test_ambiguous_first_segment_city(osm):
    x = explain_address("Аксу, мкрн Беловка, 30, 15-этаж")
    r = x["result"]
    assert r["city"] == "Аксу" and r["street_name"] == "Беловка" and r["province"]
    assert {"city_ambiguous", "province_from_ambiguous_city"} <= set(x["flags"])
    assert r["confidence"] < 0.9
