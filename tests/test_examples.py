"""Примеры из ТЗ. Запуск: python -m pytest tests -q  (или python tests/test_examples.py)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import process_record  # noqa: E402

CASES = [
    ("город Алматы/Алматы қаласы, Мкр Аксай 3а, 59, кв 70",
     {"province": "Алматы", "city": "Алматы", "street_type": "микрорайон", "street_name": "Аксай 3а",
      "house": "59", "apartment": "70"}),
    ("66квартал 12дом 69квартира",
     {"street_type": "квартал", "street_name": "66", "house": "12", "apartment": "69"}),
    ("Ул .Курильская 9 кв 1",
     {"street_type": "улица", "street_name": "Курильская", "house": "9", "apartment": "1"}),
    ("Туран 55/6 магазин береке 4подьезд 9этаж",
     {"street_name": "Туран", "house": "55/6", "entrance": "4", "floor": "9", "landmark": "магазин береке"}),
    ("Нурсити 2 микрорайон 11дом 5 подьезд 239 кв",
     {"street_type": "микрорайон", "street_name": "Нурсити 2", "house": "11", "entrance": "5", "apartment": "239"}),
    ("город Актобе/Ақтөбе қаласы, Назарбаева8, kv 12",
     {"province": "Актюбинская область", "city": "Актобе", "street_name": "Назарбаева", "house": "8",
      "apartment": "12"}),
    ("Турксибский район, Рыбинский переулок, дом 4",
     {"district": "Турксибский", "city": "Алматы", "street_type": "переулок", "street_name": "Рыбинский",
      "house": "4"}),
    ("Кулманова 133", {"street_name": "Кулманова", "house": "133"}),
    ("жуалы 19", {"street_name": "жуалы", "house": "19"}),
    ("Актюбинская область, г. Хромтау, ул. Абая 1/2, flat 191",
     {"province": "Актюбинская область", "city": "Хромтау", "street_type": "улица", "street_name": "Абая",
      "house": "1/2", "apartment": "191"}),
    ("​г.Шымкент, пр. Тауке хана 12а, 3 этаж",
     {"city": "Шымкент", "province": "Шымкент", "street_type": "проспект", "street_name": "Тауке хана",
      "house": "12а", "floor": "3"}),
    ("", {"street_name": None, "house": None}),
]


def test_examples():
    fails = []
    for raw, exp in CASES:
        got = process_record({"id": "t", "raw_address": raw})
        assert got["raw_address"] == raw
        for k, v in exp.items():
            if (got[k] or "").lower() != (v or "").lower():
                fails.append(f"{raw!r}: {k} = {got[k]!r}, ожидали {v!r}")
    assert not fails, "\n" + "\n".join(fails)


if __name__ == "__main__":
    for raw, exp in CASES:
        got = process_record({"id": "t", "raw_address": raw}, debug=True)
        bad = {k: (got[k], v) for k, v in exp.items() if (got[k] or "").lower() != (v or "").lower()}
        print("OK  " if not bad else "FAIL", raw)
        print("     ", {k: v for k, v in got.items() if v and k not in ("id", "raw_address", "_source")})
        for k, (g, v) in bad.items():
            print(f"      ✗ {k}: {g!r} != {v!r}")
