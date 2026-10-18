"""Очистка шума реальных заказов: убирать только однозначное, адрес не трогать."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.noise import strip_noise  # noqa: E402
from app.pipeline import process_record  # noqa: E402


@pytest.mark.parametrize("raw,clean", [
    ("Айгерим, 87071234567, 74 квартал", "Айгерим, 74 квартал"),
    ("ул. Абая 10, кв 5 +7 777 123 4567", "ул. Абая 10, кв 5"),
    ("Бассейная, дом 14б подъезд 5 т.8-702-555-12-12", "Бассейная, дом 14б подъезд 5"),
    ("мкрн Горторг,14,квартира 280,домофон 45", "мкрн Горторг,14,квартира 280"),
    ("ул. Абая 10, кв 5, код 1234#", "ул. Абая 10, кв 5"),
    ("ул. Абая 10, кв 5, домофон не работает", "ул. Абая 10, кв 5"),
    ("Абая 10 2 падъезд 5 етаж", "Абая 10 2 подъезд 5 этаж"),
])
def test_noise_removed(raw, clean):
    assert strip_noise(raw)[0] == clean


@pytest.mark.parametrize("raw", [
    "ул. Кодар 7, кв 8",            # «код» внутри названия
    "мкр Самал-2, 58, кв 12",
    "дом 8, кв 701",                 # короткие числа — не телефон
    "Туран 55/6 магазин береке 4подьезд 9этаж",
    "66квартал 12дом 69квартира",
    "",
])
def test_address_untouched(raw):
    assert strip_noise(raw) == (raw, [])


def test_pipeline_keeps_raw_and_ignores_phone():
    raw = "г. Алматы, ул. Жандосова 5, кв 12, 87071234567"
    r = process_record({"id": 1, "raw_address": raw}, debug=True)
    assert r["raw_address"] == raw                      # в ответе — исходная строка
    assert (r["street_name"], r["house"], r["apartment"]) == ("Жандосова", "5", "12")
    assert r["_noise"] == ["телефон: 87071234567"]


@pytest.mark.parametrize("raw", [None, 123, ["список"], {"a": 1}])
def test_non_string_is_safe(raw):
    assert strip_noise(raw) == (raw, [])
