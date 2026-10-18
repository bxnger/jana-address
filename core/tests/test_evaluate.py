"""evaluate.py: подсчёты на мини-данных, где ответ посчитан вручную."""
from evaluate import align, evaluate, norm_value

GOLD = [
    {"id": "a", "raw_address": "Алматы, Абая 1/2, кв 5", "city": "Алматы", "street_name": "Абая",
     "house": "1/2", "apartment": "5", "district": None},
    {"id": "b", "raw_address": "Абая 3", "city": None, "street_name": "Абая", "house": "3"},
    {"id": "c", "raw_address": "Ауэзовский р-н, Жандосова 9", "district": "Ауэзовский",
     "street_name": "Жандосова", "house": "9", "living_area": "Аксай"},  # Аксая нет в строке
]


def test_norm_value_follows_regulation():
    assert norm_value("  Ул. АБАЯ,  ") == "ул абая"
    assert norm_value("1/2") == "12"                       # регламент: пунктуацию убираем
    assert norm_value("1/2", strip_slash=False) == "1/2"   # --keep-slash
    assert norm_value("") is None and norm_value(None) is None


def test_counts_correct_wrong_fn_fp():
    preds = [
        {"id": "a", "city": "алматы", "street_name": "Абая", "house": "1", "apartment": None,
         "district": "Медеуский", "confidence": 0.4},
        {"id": "b", "city": "Алматы", "street_name": "Абая", "house": "3", "confidence": 0.9},
        {"id": "c", "district": "Ауэзовский", "street_name": "Жандосова", "house": "9",
         "living_area": None, "confidence": 0.95},
    ]
    r = evaluate(preds, GOLD)
    s = r["stats"]
    assert s["city"]["scored"] == 1 and s["city"]["correct"] == 1   # регистр не важен
    assert s["city"]["fp_unscored"] == 1                            # b: в эталоне null — без штрафа
    assert s["house"]["scored"] == 3 and s["house"]["wrong"] == 1   # "1" != "1/2"
    assert s["apartment"]["fn_null"] == 1 and s["apartment"]["scored"] == 1
    assert s["district"]["scored"] == 1 and s["district"]["correct"] == 1
    assert s["district"]["fp_unscored"] == 1                         # a: Медеуский при null
    assert s["living_area"]["scored"] == 0                           # "Аксай" нет в строке
    assert s["living_area"]["skipped_not_in_string"] == 1
    assert r["accuracy"]["house"] == 2 / 3
    assert r["confidence_check"]["records_with_errors"] == 1
    assert r["confidence_check"]["mean_conf_err"] == 0.4


def test_missing_and_extra_predictions():
    preds = [{"id": "b", "street_name": "Абая", "house": "3"}, {"id": "zzz", "house": "1"}]
    r = evaluate(preds, GOLD)
    assert r["missing"] == ["a", "c"]
    assert r["extra"] == 1
    assert r["stats"]["house"]["fn_null"] == 2        # пропущенные записи = null


def test_align_without_ids_uses_positions_and_does_not_crash():
    gold = [{"raw_address": "x", "house": "1"}, {"raw_address": "y", "house": "2"}]
    pairs, missing, extra = align([{"house": "1"}], gold)
    assert pairs[1][0] is None and missing == [1] and extra == []
    r = evaluate([{"house": "1", "confidence": 0.9}, {"house": "9", "confidence": 0.1}], gold)
    assert r["confidence_check"]["mean_conf_ok"] == 0.9
    assert r["confidence_check"]["mean_conf_err"] == 0.1


def test_weighted_estimate_uses_only_available_groups():
    gold = [{"id": "1", "raw_address": "Абая 3", "street_name": "Абая", "house": "3"}]
    r = evaluate([{"id": "1", "street_name": "Абая", "house": "4"}], gold)
    assert r["groups"] == {"address": 0.5}
    assert r["weighted_estimate"] == 0.5


def test_split_deterministic_no_overlap(tmp_path):
    import json, subprocess, sys
    from pathlib import Path
    recs = [{"id": f"r{i}", "raw_address": f"Абая {i}", "house": str(i)} for i in range(50)]
    src = tmp_path / "train.json"
    src.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    ev = Path(__file__).resolve().parent.parent / "evaluate.py"
    outs = []
    for d in ("a", "b"):
        subprocess.run([sys.executable, str(ev), "--split", str(src), "--split-out", str(tmp_path / d)],
                       check=True, capture_output=True)
        dev = json.loads((tmp_path / d / "dev.json").read_text(encoding="utf-8"))
        hold = json.loads((tmp_path / d / "holdout.json").read_text(encoding="utf-8"))
        outs.append((dev, hold))
    dev, hold = outs[0]
    assert outs[0] == outs[1]                                   # один seed -> одно разбиение
    assert len(hold) == 10 and len(dev) == 40
    assert not {r["id"] for r in dev} & {r["id"] for r in hold}


def test_apartment_is_not_scored():
    """Организаторы: квартира не оценивается. Ошибка в apartment не меняет оценку и не считается ошибочной записью."""
    import evaluate as ev
    gold = [{"id": "a", "raw_address": "Абая 1 кв 5", "province": None, "city": "алматы", "street_name": "Абая",
             "house": "1", "apartment": "5", "entrance": None, "floor": None, "district": None, "living_area": None}]
    ok = [dict(gold[0])]
    bad_apt = [dict(gold[0], apartment="6", confidence=0.9)]
    r_ok, r_bad = ev.evaluate(ok, gold), ev.evaluate(bad_apt, gold)
    assert "apartment" not in ev.SCORED_FIELDS and "apartment" in ev.INFO_FIELDS
    assert r_ok["weighted_estimate"] == r_bad["weighted_estimate"]
    assert r_bad["confidence_check"]["records_with_errors"] == 0
    assert r_bad["stats"]["apartment"]["wrong"] == 1          # но справочно ошибка видна
