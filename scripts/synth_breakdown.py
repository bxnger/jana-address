"""Разбор ошибок на синтетике по приёмам «порчи» + точность восстановления района.

    python scripts/gen_synthetic.py && python scripts/synth_breakdown.py

Сравнение — функцией norm_value из core/evaluate.py (правила жюри).
"""
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYN = ROOT / "data" / "synthetic"
os.environ.setdefault("JANA_GAZETTEER", str(ROOT / "data" / "core_gazetteer.json"))
sys.path[:0] = [str(ROOT), str(ROOT / "core")]

from evaluate import norm_value  # noqa: E402

from app.pipeline import process_batch  # noqa: E402

SCORED = ["province", "city", "street_name", "house", "apartment", "entrance", "floor", "living_area"]


def main(top=25, examples=2):
    inp = json.loads((SYN / "synth_input.json").read_text(encoding="utf-8"))
    gold = json.loads((SYN / "synth_gold.json").read_text(encoding="utf-8"))
    styles = json.loads((SYN / "synth_styles.json").read_text(encoding="utf-8"))
    preds = process_batch(inp, debug=True)

    per_tag = defaultdict(Counter)        # tag -> {"n", "bad"}
    bad_fields = defaultdict(Counter)     # tag -> field -> errors
    samples = defaultdict(list)
    restored = Counter()
    by_group = defaultdict(Counter)   # настоящий дом / случайный номер -> ok/wrong/null
    by_via = defaultdict(Counter)     # каким путём найден район -> ok/wrong
    for p, g in zip(preds, gold, strict=True):
        tags = styles[g["id"]]
        errs = []
        for f in SCORED:
            if g[f] is None:
                continue  # поля нет в строке — не оцениваем (как у жюри)
            if norm_value(p[f], strip_slash=False) != norm_value(g[f], strip_slash=False):
                errs.append(f)
        for t in tags:
            per_tag[t]["n"] += 1
            per_tag[t]["bad"] += bool(errs)
            for f in errs:
                bad_fields[t][f] += 1
            if errs and len(samples[t]) < examples:
                samples[t].append((g["raw_address"], {f: (p[f], g[f]) for f in errs}))
        # восстановление района справочником (района нет в строке)
        if g["district"] and "district_in_string" not in tags:
            if p["district"] is None:
                outcome = "null"
            elif norm_value(p["district"]) == norm_value(g["district"]):
                outcome = "ok"
            else:
                outcome = "wrong"
            restored[outcome] += 1
            by_group["настоящий дом OSM" if "real_building" in tags else "случайный номер"][outcome] += 1
            by_via[p.get("_district_via") or "—"][outcome] += 1

    def wrong(p, g, f):
        return g[f] and norm_value(p[f], strip_slash=False) != norm_value(g[f], strip_slash=False)

    n_bad = sum(1 for p, g in zip(preds, gold, strict=True) if any(wrong(p, g, f) for f in SCORED))
    print(f"Записей: {len(gold)}; хоть одна ошибка в оцениваемых полях: {n_bad}\n")
    print(f"{'приём':<28}{'записей':>8}{'с ошибкой':>11}   чаще всего ломается")
    rows = sorted(per_tag.items(), key=lambda kv: kv[1]["bad"] / kv[1]["n"], reverse=True)
    for t, c in rows[:top]:
        common = ", ".join(f"{f}:{n}" for f, n in bad_fields[t].most_common(3))
        print(f"{t:<28}{c['n']:>8}{c['bad'] / c['n']:>10.0%}   {common}")
    tot = sum(restored.values())
    if tot:
        print(f"\nВосстановление района по справочнику (района нет в строке, {tot} записей): "
              f"верно {restored['ok'] / tot:.0%}, неверно {restored['wrong'] / tot:.0%}, "
              f"не заполнено {restored['null'] / tot:.0%}")
        for name, c in by_group.items():
            n = sum(c.values())
            print(f"  {name:<20} {n:>5}: верно {c['ok'] / n:.0%}, неверно {c['wrong'] / n:.0%}, "
                  f"пусто {c['null'] / n:.0%}")
        print("  каким путём:", ", ".join(f"{v}: {sum(c.values())} (верно {c['ok'] / max(1, sum(c.values())):.0%})"
                                         for v, c in sorted(by_via.items(), key=lambda kv: -sum(kv[1].values()))))
    print("\nПримеры ошибок (получили, ожидали):")
    for t, _ in rows[:8]:
        for raw, d in samples[t]:
            print(f"  [{t}] {raw!r}\n      {d}")


if __name__ == "__main__":
    main()
