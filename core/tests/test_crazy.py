"""Раунд 3, п.4: баги, найденные генератором «безумных» адресов (tests/gen_crazy.py), закреплены здесь."""
import pytest

from address_parser import parse_address
from evaluate import norm_value
import gen_crazy


def check(raw, **exp):
    r = parse_address(raw, "t")
    bad = {k: (v, r[k]) for k, v in exp.items()
           if norm_value(r[k], strip_slash=False) != norm_value(v, strip_slash=False)}
    assert not bad, f"{raw!r}: {bad}"


@pytest.mark.parametrize("raw,exp", [
    # латинские ключевые слова: pod без точки не распознавался, улица терялась
    ("Абая dom 10 kv 5 etazh 3 pod 2", dict(street_name="Абая", house="10", apartment="5", floor="3", entrance="2")),
    ("ул. Толе би, dom. 12а, pod6, этаж14, кв-ра296", dict(house="12а", entrance="6", floor="14", apartment="296")),
    ("Абая 10, podezd 3, kv 7", dict(entrance="3", apartment="7")),
    # опечатки: "этж" становился квартирой, "квартра" не распознавалась
    ("Абая 10 этж 3 подьезд 2", dict(floor="3", entrance="2", apartment=None)),
    ("Абая 10, квартра 5а", dict(apartment="5а")),
    ("Абая 10, кваритра 5", dict(apartment="5")),
    ("Абая 10, етаж 4", dict(floor="4")),
    # двойная дробь: терялась вторая часть
    ("Абая 12/1/2 кв 5", dict(house="12/1/2", apartment="5")),
    ("д.12/1 кв.5а Абая", dict(house="12/1", apartment="5а", street_name="Абая")),
    # число в названии улицы: "8 Марта" терялось/обрезалось
    ("Алматы, 8 Марта, dom 12/1, кв 3", dict(street_name="8 Марта", house="12/1", apartment="3")),
    ("Караганда, 8 Марта көшесі, 15 үй", dict(street_name="8 Марта", street_type="улица", house="15")),
    ("8 Марта улица 15", dict(street_name="8 Марта", house="15")),
    ("Алматы, 8 Марта, 15", dict(street_name="8 Марта", house="15")),
    ("50 лет Октября, дом 5", dict(street_name="50 лет Октября", house="5")),
    # казахские ключевые слова
    ("Абай көшесі, 10 үй, 2 кіреберіс, 3 қабат, 45 пәтер",
     dict(street_name="Абай", house="10", entrance="2", floor="3", apartment="45")),
    ("Самал шағын ауданы 5 үй 12 пәтер", dict(street_type="микрорайон", street_name="Самал", house="5", apartment="12")),
    # невидимые символы и повтор города
    ("г. ⁠Алматы, Абая, dom 12а, этж 7, flat 152", dict(city="Алматы", house="12а", floor="7", apartment="152")),
    ("Павлодар, г.Павлодар, улица С​атпаева, dom. 33/1б, под. 8, этж. 9, квартра 201а",
     dict(city="Павлодар", street_name="Сатпаева", house="33/1б", entrance="8", floor="9", apartment="201а")),
])
def test_found_bugs(raw, exp):
    check(raw, **exp)


def test_number_before_ordinary_street_not_absorbed():
    r = parse_address("12 Абая улица")       # "12" — не часть названия (не "8 Марта")
    assert r["street_name"] == "Абая"


@pytest.mark.parametrize("seed", [2026, 7, 99])
def test_generator_sets(seed):
    """Шаблонный набор (не train.json): все поля должны совпасть. Доля на случай будущих шаблонов."""
    stats, bad = gen_crazy.run(gen_crazy.generate(300, seed))
    ok = sum(a for a, _ in stats.values())
    n = sum(b for _, b in stats.values())
    assert ok / n >= 0.99, bad[:10]
