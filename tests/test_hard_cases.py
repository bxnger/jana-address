"""Сложные адреса (data/demo/hard_cases.py): казахский, старые названия, КАПС, слитное написание,
разделители-точки, сёла, шум. Проверяются только поля, перечисленные в ожидании."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "demo"))

from hard_cases import CASES  # noqa: E402

from app.pipeline import process_record  # noqa: E402


@pytest.mark.parametrize("raw,expected", CASES, ids=[f"{i:02d}" for i in range(len(CASES))])
def test_hard_case(raw, expected):
    got = process_record({"id": 1, "raw_address": raw})
    diff = {k: (got.get(k), v) for k, v in expected.items() if (got.get(k) or None) != v}
    assert not diff, f"{raw!r}: (получено, ожидалось) {diff}"


def test_lowercase_demo_line():
    """Строчные название города и улицы приводятся к виду справочника; телефон и пометка не мешают."""
    from app.pipeline import process_record
    out = process_record({"id": "d", "raw_address":
                          "астана туран 55/6 магазин береке 4подьезд 9этаж +7 701 123 45 67 звонить за час"})
    assert out["city"] == "Астана" and out["street_name"] == "Туран"
    assert out["house"] == "55/6" and out["entrance"] == "4" and out["floor"] == "9"
    assert out["landmark"] == "магазин береке"


def test_label_prefix_and_city_typo_suggestion():
    """«запасная опечатка: Шымкнт, …» — подпись отброшена, город не подменён, но вариант предложен."""
    from app.pipeline import process_record
    out = process_record({"id": "d", "raw_address": "запасная опечатка: Шымкнт, Тауке хана 12 проверяй"}, debug=True)
    assert out["street_name"] == "Тауке хана" and out["house"] == "12"
    assert out["city"] == "Шымкнт"
    assert out["_suggestions"]["city"][0]["value"] == "Шымкент"


def test_phone_instead_of_apartment():
    """Телефон и пометка «вместо кв» не должны попадать в дом и улицу."""
    from app.pipeline import process_record
    out = process_record({"id": "d", "raw_address": "Талдыкорган Жансугурова 18, 112 комн, тел 87051234567 вместо кв"})
    assert out["city"] == "Талдыкорган" and out["street_name"] == "Жансугурова"
    assert out["house"] == "18" and out["apartment"] == "112"
