"""Справочник учится на правках: выученное заполняет пустое и не спорит со строкой."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import learning  # noqa: E402
from app.pipeline import process_record  # noqa: E402

RAW = "г. Алматы, ул. Выдуманная 7, кв 3"


def _fresh(tmp_path, monkeypatch):
    learned = learning.Learned(tmp_path / "corrections.jsonl")
    monkeypatch.setattr(learning, "_LEARNED", learned)
    return learned


def test_learns_district_for_house_and_street(tmp_path, monkeypatch):
    learned = _fresh(tmp_path, monkeypatch)
    assert process_record({"id": 1, "raw_address": RAW})["district"] is None

    out = learned.add([{"id": 1, "raw_address": RAW, "city": "Алматы", "street_name": "Выдуманная",
                        "house": "7", "district": "Медеуский"}])
    assert out["districts"] == 1

    r = process_record({"id": 2, "raw_address": RAW}, debug=True)
    assert r["district"] == "Медеуский" and r["_district_via"] == "learned_house"
    assert r["_source"]["district"] == "learned"
    # другой дом той же улицы — по улице
    r = process_record({"id": 3, "raw_address": "г. Алматы, ул. Выдуманная 99"}, debug=True)
    assert r["district"] == "Медеуский" and r["_district_via"] == "learned_street"


def test_never_overrides_district_from_text(tmp_path, monkeypatch):
    learned = _fresh(tmp_path, monkeypatch)
    learned.add([{"city": "Алматы", "street_name": "Выдуманная", "house": "7", "district": "Медеуский"}])
    r = process_record({"id": 1, "raw_address": "Алатауский район, ул. Выдуманная 7"})
    assert r["district"] == "Алатауский"


def test_persists_and_reloads(tmp_path, monkeypatch):
    learned = _fresh(tmp_path, monkeypatch)
    learned.add([{"city": "Алматы", "street_name": "Выдуманная", "house": "7", "district": "Медеуский",
                  "living_area": "Новый Квартал Тест"}])
    again = learning.Learned(tmp_path / "corrections.jsonl")
    again.load()
    assert again.district_for("Алматы", "Выдуманная", "7") == ("Медеуский", "learned_house")
    assert again.stats()["living_areas"] == 1


def test_multi_district_street_learns_only_the_house(tmp_path, monkeypatch):
    # Абая в Алматы по OSM проходит через несколько районов: правка для дома 5 не должна
    # становиться районом всего проспекта
    from app.gazetteer import get_gazetteer
    learned = _fresh(tmp_path, monkeypatch)
    learned.add([{"city": "Алматы", "street_name": "Абая", "house": "5", "district": "Наурызбайский"}],
                get_gazetteer())
    assert learned.district_for("Алматы", "Абая", "5") == ("Наурызбайский", "learned_house")
    assert learned.district_for("Алматы", "Абая", "150") == (None, None)


def test_conflicting_street_districts_are_not_guessed(tmp_path, monkeypatch):
    learned = _fresh(tmp_path, monkeypatch)
    learned.add([{"city": "Алматы", "street_name": "Выдуманная", "house": "1", "district": "Медеуский"},
                 {"city": "Алматы", "street_name": "Выдуманная", "house": "2", "district": "Алатауский"}])
    assert learned.district_for("Алматы", "Выдуманная", "5") == (None, None)
    assert learned.district_for("Алматы", "Выдуманная", "2") == ("Алатауский", "learned_house")
