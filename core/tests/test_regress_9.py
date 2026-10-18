"""Адреса от команды: массив, «под 2», «ориентир …», «519 / 521», кавычки."""
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
    ("Шымкент қаласы, Еңбекші район, жи массив Сайрам, ул .Толе би 45/1",
     {"living_area": "Сайрам", "street_name": "Толе би", "house": "45/1"}),
    ("Жамбылская область, г.Тараз, массив Карасу, ул. Шыгыс 2, д.18",
     {"living_area": "Карасу", "street_name": "Шыгыс", "house": "18"}),
    ("г.Костанай, ул Байтурсынова 67дом, под 2, кв 89", {"house": "67", "entrance": "2", "apartment": "89"}),
    ("Мкр. Шыгыс-2, дом 12, кв 34, ориентир Qazpost Солнечный",
     {"street_name": "Шыгыс-2", "landmark": "Qazpost Солнечный", "house": "12"}),
    ("Жетысуский район, пр. Сейфуллина 519 / 521, уг. ул. Жибек Жолы",
     {"street_name": "Сейфуллина", "house": "519/521", "apartment": None}),
    ("г. Кызылорда, ул. Жибек Жолы 11А, магазин 'Светлана'",
     {"street_name": "Жибек Жолы", "house": "11А", "landmark": "магазин Светлана"}),
]


@pytest.mark.parametrize("raw,exp", CASES)
def test_team_cases(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)


CASES_40 = [
    ("г. Алматы, мкр-н. Самал-2, д. 48/57, кв. б/н",
     {"street_type": "микрорайон", "street_name": "Самал-2", "house": "48/57", "apartment": None}),
    ("г.Шымкент, Абайский р-н, ул. К.Аманжолова, б\\н, под. 3, эт. 4",
     {"street_name": "К.Аманжолова", "house": None, "entrance": "3", "floor": "4"}),
    ("г. Петропавловск, ул.Мира б/н, ориентир аптека №3",
     {"street_name": "Мира", "house": None, "landmark": "аптека №3"}),
    ("г. Актау, 14 микрорайон, зд. 3Б, кв 45", {"street_name": "14", "house": "3Б", "apartment": "45"}),
    ("г. Павлодар, ул. Ломова, д. 149, общежитие №2", {"house": "149", "landmark": "общежитие №2"}),
]


@pytest.mark.parametrize("raw,exp", CASES_40)
def test_team_cases_40(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)


CASES_HARD = [
    ("Усть-КаменогорскУтегенбатыра8кв84под9эт7", {"city": "Усть-Каменогорск", "street_name": "Утегенбатыра", "house": "8"}),
    ("Петропавловск, Бокейхана 89, кв 81-169", {"house": "89", "apartment": "81"}),
    ("БЦ Каусар, второй этаж", {"floor": "2", "street_name": None, "landmark": "БЦ Каусар"}),
    ("Жезказган, Момышулы 22 (1-2 подъезды)", {"house": "22", "apartment": None, "entrance": "1"}),
    ("Экибастуз, Достык 77, корп. 9.2", {"house": "77", "apartment": None}),
    ("Almaty q., Момышулы kosh., 20 uy", {"city": "Алматы", "street_name": "Момышулы", "house": "20"}),
    ("Атырау, Айтиева, ранее Фурманова, дом 81а", {"street_name": "Айтиева", "house": "81а"}),
    ("Караганда, Қазыбек би 10А-2", {"street_name": "Қазыбек би", "house": "10А"}),
    ("Кызылорда, Бокейхана, дом номер сорок третий", {"street_name": "Бокейхана", "house": None}),
    ("индекс для Тараз, но фактически Кызылорда, Достык 166", {"city": "Кызылорда", "house": "166"}),
    ("Нур-Султан (ныне Астана), Сейфуллина 51", {"city": "Астана", "house": "51"}),
]


@pytest.mark.parametrize("raw,exp", CASES_HARD)
def test_hard_batch(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)
