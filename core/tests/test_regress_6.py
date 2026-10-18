"""имя получателя и пометки курьеру — не адрес.
Строки из задания Ерасыла и свои (имена и форматы, которых нет в его генераторе).
Часть проверок опирается на OSM-справочник (мкр «Айгерим», улица «Батыр») — они в фикстуре osm."""
from pathlib import Path

import pytest

import gazetteer as gz
from address_parser import explain_address, parse_address

REAL = Path(__file__).resolve().parents[2] / "data" / "core_gazetteer.json"


def ex(raw):
    x = explain_address(raw)
    return x["result"], x["extras"], x["leftover"]


@pytest.fixture
def osm():
    if not REAL.is_file():
        pytest.skip("нет data/core_gazetteer.json")
    snap, err = gz._snapshot(), gz.EXTERNAL_ERROR
    assert gz.load_external(REAL)
    yield
    gz._restore(snap)
    gz.EXTERNAL_ERROR = err


@pytest.mark.parametrize("raw,street,house,who", [
    ("ул. Абая 10, кв 5, получатель Ержан", "Абая", "10", "Ержан"),
    ("для Асель, г. Павлодар, мкрн Горторг, 14", "Горторг", "14", "Асель"),
    ("звонить Нурлану, г. Кызылорда, мкрн Саяхат, 38, кв. 71", "Саяхат", "38", "Нурлану"),
    ("Туркестан С.Асанов 15 кв. 70, получатель: Райхан", "С.Асанов", "15", "Райхан"),
    ("г. Алматы ул Жибек жолы 50 кв 12 спросить Асель Маратовну", "Жибек жолы", "50", "Асель Маратовну"),
    ("кому: Бауыржан, улица бухар жырау д. 4/2 13-этаж", "бухар жырау", "4/2", "Бауыржан"),
    ("ЖАМБЫЛСКАЯ ОБЛАСТЬ, Г. ТАРАЗ, СУЛЕЙМЕНОВА, Д. 129В, 45КВ, ЗВОНИТЬ НУРЛАНУ", "СУЛЕЙМЕНОВА", "129В", "НУРЛАНУ"),
])
def test_recipient_after_marker(raw, street, house, who):
    r, extras, left = ex(raw)
    assert (r["street_name"], r["house"]) == (street, house)
    assert extras.get("recipient") == who and not left


@pytest.mark.parametrize("raw,street,city,who", [
    ("Мадина, Шымкент Мкр Жанаталап, 23 1-й подъезд 171квартира", "Жанаталап", "Шымкент", "Мадина"),
    ("Айгерим, 74 квартал, д. 18 21квартира", "74", None, "Айгерим"),
    ("Эвелина Ахметов, г. Сарыагаш, 62 квартал, д. 1, 6кв", "62", "Сарыагаш", "Эвелина Ахметов"),
    ("Наргиз, город Тараз, квартал 110, 16", "110", "Тараз", "Наргиз"),
    ("г.Сатпаев, жанпеисова, д. 15/1, kv 147, подъезд 6, Ерзат", "жанпеисова", "Сатпаев", "Ерзат"),
    ("Рудный, Ул. Лесопарковая 15/8, кв-280, Серик", "Лесопарковая", "Рудный", "Серик"),
])
def test_bare_name_segment(raw, street, city, who):
    r, extras, _ = ex(raw)
    assert (r["street_name"], r["city"]) == (street, city)
    assert extras.get("recipient") == who


@pytest.mark.parametrize("raw,street,name_field", [
    ("Мухтара Ауэзова, дом 7 кв.247, получатель Гульдана", "Мухтара Ауэзова", None),  # родительный падеж — улица
    ("Абай, ул. Кунаева 5", "Кунаева", "Абай"),            # имя-город остаётся городом
])
def test_names_that_are_address(raw, street, name_field):
    r, extras, _ = ex(raw)
    assert r["street_name"] == street
    assert r["city"] == name_field or name_field is None
    assert "Мухтара" not in (extras.get("recipient") or "")


@pytest.mark.parametrize("raw,street,city", [
    ("Ахмета Байтурсынова, г. Алматы, 15", "Ахмета Байтурсынова", "Алматы"),   # улица по справочнику
    ("Серик, ул. Абая 5", "Абая", "Серик"),                                   # посёлок Серик по справочнику
])
def test_names_that_are_address_by_gazetteer(osm, raw, street, city):
    r, extras, _ = ex(raw)
    assert (r["street_name"], r["city"]) == (street, city) and not extras


def test_living_area_named_like_person_kept(osm):
    r, extras, _ = ex("г. Алматы, Айгерим, 15")
    assert r["street_name"] == "Айгерим" and r["house"] == "15" and not extras


def test_name_after_house_removed_even_if_living_area(osm):
    r, extras, _ = ex("г. Алматы, ул. Абая 5, Айгерим")
    assert r["street_name"] == "Абая" and extras.get("recipient") == "Айгерим"


@pytest.mark.parametrize("raw,note", [
    ("оставить у двери, г. Атырау, мкр Атамұра, 17/8", "оставить у двери"),
    ("19квартал 3дом, 12 эт, 161квартира, оставить у двери", "оставить у двери"),
    ("г. Алматы, ул. Абая 5, с 9 до 18", "с 9 до 18"),
    ("г. Алматы, ул. Абая 5, позвонить за час", "позвонить за час"),
    ("г. Алматы, ул. Абая 5, кв 3, позвонить заранее", "позвонить заранее"),
])
def test_courier_note(raw, note):
    r, extras, left = ex(raw)
    assert extras.get("courier_note") == note and not left
    assert r["street_name"] in ("Абая", "Атамұра", "19")


def test_contract_unchanged_by_extras():
    r = parse_address("ул. Абая 10, кв 5, получатель Ержан", "x")
    assert "recipient" not in r and len(r) == 14


def test_guess_lowers_confidence():
    a = parse_address("г.Сатпаев, жанпеисова, д. 15/1, kv 147", "a")["confidence"]
    b = parse_address("г.Сатпаев, жанпеисова, д. 15/1, kv 147, Ерзат", "b")["confidence"]
    assert b < a


# --- ошибки, найденные прогоном по сложным случаям из PDF и свежей синтетике ---------
@pytest.mark.parametrize("raw,exp", [
    ("Алматинская область, Илийский район, п. Боралдай, ул. Ленина 5", dict(city="Боралдай", street_name="Ленина")),
    ("Кызылординская область, Шиелийский район, п. Шиели, ул. Кожанова 5", dict(city="Шиели")),
    ("ул Абая 5, п. 3, кв 7", dict(street_name="Абая", house="5", entrance="3", apartment="7")),
    ("Байконур, Ул .в. п. бармина,дом 25в 4 подьезд", dict(city="Байконур", street_name="в. п. бармина", house="25в")),
    ("Туркестан С.Асанов 15 кв. 70", dict(city="Туркестан", street_name="С.Асанов", house="15")),
    ("Астана, ул. 23-15, д. 4", dict(city="Астана", street_name="23-15", house="4")),
    ("Караганда, Юго-Восток, мкр Степной-2, 7", dict(living_area="Юго-Восток", street_name="Степной-2", house="7")),
    ("микрорайон Саяхат, Ынтымак, 18в, кв 245", dict(living_area="Саяхат", street_name="Ынтымак", house="18в")),
    ("Мкр Арищенко, ул.Луначарского д.3 11-й подъезд", dict(living_area="Арищенко", street_name="Луначарского")),
    ("город Алматы, Блока, 76дом", dict(city="Алматы", street_name="Блока", house="76")),
    ("Ул. дінмұхамед қонаев, д.19б, 38квартира", dict(street_name="дінмұхамед қонаев", house="19б", city=None)),
    ("Кіші Бурыл дом 223", dict(street_name="Кіші Бурыл", house="223", city=None)),
    ("Мамыр-4 дом 101 кв 22 Алматы", dict(city="Алматы", street_name="Мамыр-4", house="101")),
    ("Курмангазы дом 8, под. 7", dict(street_name="Курмангазы", house="8", city=None)),
    ("Атырау, 11, 13этаж", dict(city="Атырау", house="11")),           # крупный город остаётся городом
])
def test_fixes(raw, exp):
    r = parse_address(raw, "t")
    bad = {k: (v, r[k]) for k, v in exp.items() if r[k] != v}
    assert not bad, f"{raw!r}: {bad}"


@pytest.mark.parametrize("raw,exp", [
    ("Алматы, Шанырак 2, ул. Жанкожа батыра 202", dict(living_area="Шанырак 2", street_name="Жанкожа батыра", house="202")),
    ("Алматы, Коктем 2, ул.Байрона дом 49", dict(living_area="Коктем 2", street_name="Байрона", house="49")),
    ("Аксай 3 микрорайон 47дом", dict(city=None, street_name="Аксай 3", house="47")),
    ("Жетысуская область, г. Карабулак, Базарбая Асанова, 181 10 подьезд", dict(street_name="Базарбая Асанова", house="181")),
    ("г. Атырау, базар жырау 89, 254кв", dict(street_name="базар жырау", landmark=None)),
    ("Астана, р-н Есиль, ул. Сыганак 18/2", dict(district="Есиль", street_name="Сыганак")),
    ("Шымкент қаласы, Абай ауданы, Тауке хан даңғылы 5", dict(district="Абайский", street_type="проспект")),
    ("Алматы қаласы, Әуезов ауданы, Жетісу-2 шағын ауданы, 45 үй", dict(district="Ауэзовский", street_name="Жетісу-2")),
    ("Астана, Сарыарка район, Сейфуллина 31", dict(district="Сарыарка")),          # русское написание не трогаем
    ("Алматы, Орбита-2, 14/1, кв 40", dict(street_type="микрорайон", street_name="Орбита-2")),
    ("Серик, Рудный, Мкр Восточный, 3 кв-79", dict(city="Рудный", street_name="Восточный")),
    ("Байконыр, Гагарина 5", dict(city="Байконур", province="Кызылординская область")),
])
def test_fixes_osm(osm, raw, exp):
    r = parse_address(raw, "t")
    bad = {k: (v, r[k]) for k, v in exp.items() if r[k] != v}
    assert not bad, f"{raw!r}: {bad}"
