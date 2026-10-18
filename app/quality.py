"""Автоматический анализ качества: точность по полям, частые ошибки, сколько адресов на проверку, скорость.

Сравнение по правилам жюри: нижний регистр, без пунктуации и лишних пробелов.
Поле, которого нет в эталоне, не оценивается (как у жюри). Квартира не входит в метрику
(ответ организаторов), но показывается справочно.
"""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict

FIELDS = ["province", "city", "district", "living_area", "street_type", "street_name",
          "house", "apartment", "entrance", "floor", "landmark"]
GROUPS = {
    "address": (0.60, ["province", "city", "street_name", "house"]),
    "details": (0.25, ["entrance", "floor"]),
    "reference": (0.15, ["district", "living_area"]),
}
INFO_FIELDS = ["apartment", "street_type", "landmark"]   # не в метрике — справочно


def norm_value(v) -> str:
    if v is None:
        return ""
    s = str(v).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compare(preds: list[dict], gold: list[dict], max_examples: int = 5) -> dict:
    """Предсказания против эталона (формат sample_output.json). Пары — по id, иначе по позиции."""
    by_id = {str(g.get("id")): g for g in gold}
    use_id = len(by_id) == len(gold) and all(str(p.get("id")) in by_id for p in preds)
    pairs = [(p, by_id[str(p.get("id"))]) for p in preds] if use_id else list(zip(preds, gold, strict=False))

    stats = {f: Counter() for f in FIELDS}
    errors = defaultdict(list)
    bad_records = 0
    for p, g in pairs:
        wrong_here = False
        for f in FIELDS:
            gv = g.get(f)
            if gv in (None, ""):
                continue                      # нет в эталоне — не оценивается
            if norm_value(p.get(f)) == norm_value(gv):
                stats[f]["ok"] += 1
            else:
                stats[f]["null" if p.get(f) in (None, "") else "wrong"] += 1
                if f not in INFO_FIELDS:
                    wrong_here = True
                if len(errors[f]) < max_examples:
                    errors[f].append({"raw_address": g.get("raw_address"), "got": p.get(f), "expected": gv})
        bad_records += wrong_here

    acc = {}
    for f, c in stats.items():
        n = c["ok"] + c["wrong"] + c["null"]
        acc[f] = {"total": n, "ok": c["ok"], "wrong": c["wrong"], "null": c["null"],
                  "accuracy": round(c["ok"] / n, 4) if n else None}
    groups = {}
    for name, (w, fs) in GROUPS.items():
        vals = [acc[f]["accuracy"] for f in fs if acc[f]["accuracy"] is not None]
        groups[name] = {"weight": w, "accuracy": round(sum(vals) / len(vals), 4) if vals else None}
    have = [g for g in groups.values() if g["accuracy"] is not None]
    score = (round(sum(g["weight"] * g["accuracy"] for g in have) / sum(g["weight"] for g in have), 4)
             if have else None)
    top = sorted(((f, a["wrong"] + a["null"]) for f, a in acc.items() if f not in INFO_FIELDS),
                 key=lambda x: -x[1])
    return {"records": len(pairs), "matched_by": "id" if use_id else "position", "score": score,
            "groups": groups, "fields": acc, "records_with_errors": bad_records,
            "top_error_fields": [{"field": f, "errors": n} for f, n in top if n][:5],
            "examples": dict(errors)}


def batch_summary(results: list[dict], elapsed: float, threshold: float = 0.7) -> dict:
    """Сводка по разобранному пакету без эталона: заполненность, на проверку, причины, скорость."""
    n = len(results)
    fill = {f: round(sum(1 for r in results if r.get(f)) / n, 4) if n else 0 for f in FIELDS}
    review = [r for r in results if (r.get("confidence") or 0) < threshold]
    reasons = Counter(f for r in review for f in (r.get("_doubt") or []))
    return {"records": n, "needs_review": len(review), "needs_review_share": round(len(review) / n, 4) if n else 0,
            "fill_rate": fill, "doubt_fields": dict(reasons.most_common()),
            "seconds": round(elapsed, 3), "seconds_per_1000": round(elapsed / n * 1000, 3) if n else None}


def timed(fn, *args, **kwargs):
    t = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, time.perf_counter() - t
