"""Контракт parse_address / parse_batch — то, что ломать нельзя."""
import json
import time

import pytest

from address_parser import FIELD_ORDER, parse_address, parse_batch

RAWS = [
    "город Алматы/Алматы қаласы, Мкр Аксай За, 59, кв 70",
    "Туран 55/6 магазин береке 4подьезд 9этаж",
    "  ул.  Абая ,  15 ,кв.  3  ",
    "Абая​ 15 кв 3﻿",
    "\t\n",
    "",
    "????",
    "1/2",
    "кв кв кв подъезд этаж дом",
    "а" * 5000,
]


def test_field_order_and_types():
    r = parse_address("ул. Абая 1/2 кв 5", "rec_1")
    assert list(r.keys()) == FIELD_ORDER
    assert r["id"] == "rec_1"
    for f in FIELD_ORDER:
        if f in ("id", "raw_address", "confidence"):
            continue
        assert r[f] is None or isinstance(r[f], str), f
    assert isinstance(r["confidence"], float) and 0.0 <= r["confidence"] <= 1.0


@pytest.mark.parametrize("raw", RAWS)
def test_raw_address_unchanged_and_no_crash(raw):
    r = parse_address(raw, "x")
    assert r["raw_address"] == raw  # байт-в-байт, включая невидимые символы и пробелы
    assert 0.0 <= r["confidence"] <= 1.0


@pytest.mark.parametrize("bad", [None, 123, 12.5, ["ул", "Абая"]])
def test_non_string_input_does_not_crash(bad):
    r = parse_address(bad, "x")
    assert r["raw_address"] == bad
    assert r["confidence"] <= 0.1


def test_house_is_string_fraction_preserved():
    r = parse_address("ул. Абая 1/2")
    assert r["house"] == "1/2" and r["house"] != "1"


def test_batch_count_order_and_ids():
    records = [{"id": f"rec_{i}", "raw_address": raw} for i, raw in enumerate(RAWS)]
    records.append({"id": "no_raw"})              # запись без raw_address
    records.append({"raw_address": "Абая 5"})     # запись без id
    out = parse_batch(records)
    assert len(out) == len(records)
    for rec, res in zip(records, out):
        assert res["id"] == rec.get("id")
        assert res["raw_address"] == rec.get("raw_address")


def test_empty_batch():
    assert parse_batch([]) == []


def test_unparseable_gets_nulls_and_low_confidence():
    r = parse_address("????", "x")
    assert all(r[f] is None for f in FIELD_ORDER if f not in ("id", "raw_address", "confidence"))
    assert r["confidence"] <= 0.1


def test_json_serializable():
    out = parse_batch([{"id": "a", "raw_address": RAWS[0]}])
    json.dumps(out, ensure_ascii=False)


def test_no_lookup_by_id():
    """Одинаковая строка с разными id даёт одинаковый разбор (id не влияет)."""
    a = parse_address(RAWS[0], "rec_00417")
    b = parse_address(RAWS[0], "zzz")
    a.pop("id"), b.pop("id")
    assert a == b


def test_fuzz_internal_parser_never_raises():
    """parse_address глушит исключения (запись не должна теряться), поэтому
    fuzz гоняем по внутренней _parse — чтобы баги не прятались."""
    import random
    from address_parser import _parse
    rnd = random.Random(42)
    alphabet = ("абвгдеёжзиклмнопрстуфхцчшщъыьэюяәғқңөұүһі АБВ0123456789/-.,№#"
                "кв ул мкр д эт подъезд г. область район ЖК​\xa0()«»")
    for _ in range(3000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 80)))
        _parse(s)


def test_speed_1000_records():
    base = [
        "город Алматы/Алматы қаласы, Мкр Аксай За, 59, кв 70",
        "Туран 55/6 магазин береке 4подьезд 9этаж",
        "Алматинская обл., Карасайский р-н, с. Каскелен, ул. Наурызбай батыра 5",
        "Нурсити 2 микрорайон 11дом 5 подьезд 239 кв",
    ]
    records = [{"id": str(i), "raw_address": base[i % len(base)] + f" {i % 7}"} for i in range(1000)]
    t0 = time.perf_counter()
    out = parse_batch(records)
    dt = time.perf_counter() - t0
    assert len(out) == 1000
    assert dt < 30, f"1000 записей за {dt:.1f} c (лимит регламента 300 c)"
