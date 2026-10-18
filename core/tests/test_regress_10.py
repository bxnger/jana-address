"""Адреса, написанные живыми людьми: пометки курьеру, общежития, комнаты, старые названия."""
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
    ("Кенесары д 8 кв 140 этаж 12 лифт слева",
     {"street_name": "Кенесары", "house": "8", "apartment": "140", "floor": "12"}),
    ("Рядом метро нет, но это возле Хан Шатыра, Достык 5 кв 21",
     {"city": None, "street_name": "Достык", "house": "5", "apartment": "21", "landmark": "возле Хан Шатыра"}),
    ("обл Акмолинская но живу в Астане, Бейбитшилик 36 кв 9",
     {"city": "Астана", "province": "Астана", "street_name": "Бейбитшилик", "house": "36"}),
    ("Достык 162 ЖК Botanika 2 очередь кв 303 охрана на входе спросить",
     {"city": None, "street_name": "Достык", "house": "162", "apartment": "303"}),
    ("Фурманова (Айтиева) 90 кв 14", {"city": None, "street_name": "Фурманова", "house": "90", "apartment": "14"}),
    ("Абая угол Байтурсынова, желтый дом, кв 5, этаж 2",
     {"city": None, "street_name": "Абая", "apartment": "5", "floor": "2", "landmark": "угол Байтурсынова"}),
    ("Толе би 221 общ номер 2 комната 309",
     {"street_name": "Толе би", "house": "221", "apartment": "309", "landmark": "общежитие №2"}),
    ("Жамбыла 25 общ 1 к 115 дежурная на входе",
     {"city": None, "street_name": "Жамбыла", "house": "25", "apartment": "115", "landmark": "общежитие №1"}),
    ("пр. Тауке хана 15 кв квартира 301 домофон 301",
     {"street_name": "Тауке хана", "house": "15", "apartment": "301"}),
    ("Бухар жырау 48 кв3 код подъезда 17К9", {"street_name": "Бухар жырау", "house": "48", "entrance": None}),
    ("Степной 2, Назарбаева 115, общежитие КарТУ к 4 комн 208",
     {"living_area": "Степной 2", "street_name": "Назарбаева", "house": "115", "apartment": "208"}),
    ("Пришахтинск, Абая 22 кв 2 дом с магазином внизу",
     {"street_name": "Абая", "house": "22", "apartment": "2"}),
    ("Санкибай батыра 51 кв 110, лифт не работает 5 этаж пешком",
     {"city": None, "street_name": "Санкибай батыра", "house": "51", "apartment": "110", "floor": "5"}),
    ("District Молокова 112 кв 48 заезд со двора шлагбаум",
     {"street_name": "Молокова", "house": "112", "apartment": "48"}),
    ("Кентау трассасы бойы, Фармановский рынок жанында, 5 кв",
     {"city": None, "landmark": "Фармановский рынок жанында", "apartment": "5"}),
    ("Розыбакиева угол Утепова, двор со стороны Фармамира, кв 5",
     {"city": None, "street_name": "Розыбакиева", "apartment": "5"}),
    ("Сарыарка 12/1 кв56 домофон не работает заходите за охранником",
     {"street_name": "Сарыарка", "house": "12/1", "apartment": "56"}),
]


@pytest.mark.parametrize("raw,exp", CASES)
def test_live_addresses(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)


CASES_2 = [
    ("Гоголя 15 дом свой забор зеленый калитка слева", {"street_name": "Гоголя", "house": "15"}),
    ("возле Магнум на Чехова дом 9 кв 15 если не открою я на работе",
     {"street_name": "Чехова", "house": "9", "apartment": "15", "landmark": "возле Магнум"}),
    ("rjcnfyfq abai 14 kv 5", {"city": "Костанай", "house": "14", "apartment": "5"}),
    ("gtnhjgfdkjdcr 'fv,skf 9", {"city": "Петропавловск", "street_name": "Жамбыла", "house": "9"}),
    ("'tprfpufy cntgyfz 3", {"city": "Жезказган", "street_name": "Степная", "house": "3"}),
    ("СНТ Ромашка участок 45 по трассе на Рудный", {"street_name": "Ромашка", "house": "45"}),
    ("рынок Казпотребсоюз рядом Тарана 100 кв 5",
     {"street_name": "Тарана", "house": "100", "apartment": "5", "landmark": "рынок Казпотребсоюз"}),
    ("Ленина 7 она теперь Академика Сатпаева кв 20", {"street_name": "Академика Сатпаева", "house": "7"}),
    ("в 2гис показывает ЖК Атамекен а по факту Кутузова 50", {"street_name": "Кутузова", "house": "50"}),
    ("общежитие №3 ПГУ комн 212 этаж 2", {"street_name": None, "apartment": "212", "landmark": "общежитие №3 ПГУ"}),
    ("рядом Казгидромет Айманова 9 кв 1", {"street_name": "Айманова", "house": "9", "landmark": "рядом Казгидромет"}),
    ("6 мкр дом 12 кв 45 этаж 9 лифт грузовой", {"street_name": "6", "house": "12", "floor": "9"}),
    ("старый город Азербаева 3 калитка зеленая", {"city": None, "street_name": "Азербаева", "house": "3"}),
    ("Islam Каримова 9, название улицы сама не уверена как правильно пишется",
     {"city": None, "street_name": "Islam Каримова", "house": "9"}),
    ("5 мкр дом 3 подъезд со двора а не с улицы", {"street_name": "5", "house": "3"}),
    ("адрес тот же что на Каспи но без кв так как частный сектор", {"street_name": None, "house": None}),
    ("Баймуханова 101 общежитие АГУ ком 15", {"house": "101", "apartment": "15", "landmark": "общежитие АГУ"}),
]


@pytest.mark.parametrize("raw,exp", CASES_2)
def test_live_addresses_2(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)


CASES_3 = [
    ("мкр Айша биби дом 9 кв 60 этаж 9 лифт по четным дням не ходит",
     {"street_name": "Айша биби", "house": "9", "apartment": "60", "floor": "9"}),
    ("мкр Восток-3 дом 2 кв 150 домофон на английском не понимаю кнопки",
     {"street_name": "Восток-3", "house": "2", "apartment": "150"}),
    ("мкр Молодежный дом 5 кв 40 этаж 2 вход со двора а не с фасада",
     {"street_name": "Молодежный", "house": "5", "floor": "2"}),
    ("мкр Юбилейный дом 14 кв 90 домофон отвечает соседям перепутан номер",
     {"street_name": "Юбилейный", "house": "14", "apartment": "90"}),
    ("мкр Гагарина дом 3 кв 75 этаж 9 лифт тесный вещи не влезут",
     {"street_name": "Гагарина", "house": "3", "floor": "9"}),
    ("Казахстан 85 кв 310 Усть-Каменогорск сокращенно УКК так и пишу",
     {"city": "Усть-Каменогорск", "street_name": "Казахстан", "house": "85", "apartment": "310"}),
    ("Қорқыт ата 9 жатақхана ҚМУ блок 2 бөлме 110",
     {"street_name": "Қорқыт ата", "house": "9", "apartment": "110", "landmark": "жатақхана ҚМУ"}),
    ("Әйтеке би 55 пәтер 8 ескі атауы Советская", {"street_name": "Әйтеке би", "house": "55", "apartment": "8"}),
    ("Тараз, Қойкелді 101; кв 15;;", {"city": "Тараз", "street_name": "Қойкелді", "house": "101", "apartment": "15"}),
]


@pytest.mark.parametrize("raw,exp", CASES_3)
def test_live_addresses_3(osm, raw, exp):
    r = parse_address(raw, "t")
    for k, v in exp.items():
        assert r[k] == v, (raw, k, r[k], v)
