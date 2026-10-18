"""Пометки курьеру: получатель, телефон, домофон, пометка — не адрес и не идут в ответ жюри."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import FIELDS, process_record  # noqa: E402

RAW = "г. Алматы, ул. Абая 10, кв 5, получатель Ержан, тел 87011234567, домофон 45, позвонить за час"


def test_courier_info_in_debug():
    r = process_record({"id": "c1", "raw_address": RAW}, debug=True)
    assert (r["street_name"], r["house"], r["apartment"]) == ("Абая", "10", "5")
    c = r["_courier"]
    assert c["recipient"] == "Ержан" and c["note"] == "позвонить за час"
    assert "87011234567" in c["phone"] and "45" in c["intercom"]
    assert not r["_leftover"]


def test_jury_format_has_no_extra_keys():
    r = process_record({"id": "c1", "raw_address": RAW})
    assert list(r) == ["id", "raw_address", *FIELDS, "confidence"]


def test_kazakh_district_marked_as_from_text():
    r = process_record({"id": "k", "raw_address": "Шымкент қаласы, Абай ауданы, Тауке хан даңғылы 5"}, debug=True)
    assert r["district"] == "Абайский" and r["_source"]["district"] == "text"
