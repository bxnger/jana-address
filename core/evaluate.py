#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёт по точности разбора адресов по правилам Challenge 01 (Jana Address).

Режимы:
  1) Есть эталон (sample_output.json или своя разметка):
       python evaluate.py --input sample_input.json --gold sample_output.json
  2) Синтетический эталон команды (tests/cases.json):
       python evaluate.py --synthetic
  3) Нет эталона (train.json) — диагностика без выдуманных метрик:
       python evaluate.py --input train.json --diagnose
  Сравнить другую версию парсера:  --parser path/to/address_parser.py

Правила сравнения (из регламента, стр. 10):
  - каждое поле отдельно; нижний регистр, без пунктуации и лишних пробелов;
  - падежи и опечатки не прощаются; номера сравниваются как строки;
  - поле, которого нет в эталоне (null), в подсчёт не входит —
    предсказанное значение там показывается как FP справочно, без штрафа;
  - district / living_area засчитываются только если они есть в строке
    (здесь: значение эталона встречается в нормализованной raw_address).
Веса: 60% province/city/street_name/house, 25% entrance/floor,
15% district/living_area. Квартиру (apartment) организаторы на чекпоинте исключили из оценки:
она считается и показывается справочно ("вне метрики"), но в оценку и в проверку confidence не входит. Как именно жюри агрегирует поля внутри группы, в
регламенте не сказано — здесь среднее по полям группы (это оценка, не
официальная формула).

По умолчанию удаляется ВСЯ пунктуация, как написано в регламенте ("1/2" -> "12").
--keep-slash сохраняет "/" (строже, чем регламент; полезно для отладки домов).
Записи эталона без ответа считаются ответом с null во всех полях (регламент:
"пропускать записи нельзя"); лишние ответы показываются отдельно.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent

GROUPS = {
    "address": (0.60, ["province", "city", "street_name", "house"]),
    "details": (0.25, ["entrance", "floor"]),   # квартиру организаторы не оценивают (ответ на чекпоинте)
    "reference": (0.15, ["district", "living_area"]),
}
SCORED_FIELDS = [f for _, fs in GROUPS.values() for f in fs]
INFO_FIELDS = ["apartment", "street_type", "landmark"]  # в метрику не входят, показываем справочно


# --------------------------------------------------------------------------
# загрузка
# --------------------------------------------------------------------------

def load_parser(path: str | None):
    path = Path(path) if path else HERE / "address_parser.py"
    spec = importlib.util.spec_from_file_location(f"parser_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    # свой gazetteer.py рядом с парсером (для честного «до/после» у baseline — его собственный)
    saved = sys.modules.pop("gazetteer", None)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
        if saved is not None:
            sys.modules["gazetteer"] = saved
    return mod


def load_records(path: str) -> list[dict]:
    """JSON-список, JSON-объект со списком внутри, или JSONL."""
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("records", "data", "items", "addresses", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    raise ValueError(f"Не понял формат файла {path}")


def load_synthetic(path: str | None = None) -> tuple[list[dict], list[dict]]:
    data = json.loads(Path(path or HERE / "tests" / "cases.json").read_text(encoding="utf-8"))
    inputs, gold = [], []
    for c in data["cases"]:
        inputs.append({"id": c["id"], "raw_address": c["raw"]})
        g = {"id": c["id"], "raw_address": c["raw"]}
        g.update(c.get("expected", {}))
        gold.append(g)
    return inputs, gold


# --------------------------------------------------------------------------
# сравнение
# --------------------------------------------------------------------------

def norm_value(value, strip_slash: bool = True):
    """Нормализация для сравнения по регламенту: нижний регистр, без пунктуации,
    без лишних пробелов. strip_slash=False — сохранить "/" (режим --keep-slash)."""
    if value is None:
        return None
    s = str(value).lower()
    keep = "" if strip_slash else "/"
    s = "".join(
        ch for ch in s
        if not unicodedata.category(ch).startswith("P") or ch in keep
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def in_raw(gold_value, raw, strip_slash=True) -> bool:
    g, r = norm_value(gold_value, strip_slash), norm_value(raw, strip_slash)
    return bool(g and r and g in r)


def align(preds: list[dict], gold: list[dict]):
    """Пары (pred, gold). По id, если id уникальны и заполнены в обоих списках;
    иначе по позиции. Возвращает (pairs, missing_ids, extra_preds)."""
    gid = [g.get("id") for g in gold]
    pid = [p.get("id") for p in preds]
    ids_ok = (None not in gid and len(set(gid)) == len(gid)
              and None not in pid and len(set(pid)) == len(pid))
    if ids_ok:
        by_id = {p.get("id"): p for p in preds}
        pairs = [(by_id.get(g.get("id")), g) for g in gold]
        missing = [g.get("id") for g in gold if g.get("id") not in by_id]
        gold_ids = set(gid)
        extra = [p for p in preds if p.get("id") not in gold_ids]
    else:
        pairs = [(preds[i] if i < len(preds) else None, g) for i, g in enumerate(gold)]
        missing = [g.get("id", i) for i, g in enumerate(gold) if i >= len(preds)]
        extra = preds[len(gold):]
    return pairs, missing, extra


def evaluate(preds: list[dict], gold: list[dict], strip_slash=True,
             reference_only_in_string=True) -> dict:
    pairs, missing, extra = align(preds, gold)

    stats = {f: Counter() for f in SCORED_FIELDS + INFO_FIELDS}
    errors = {f: [] for f in SCORED_FIELDS + INFO_FIELDS}
    rec_has_error = []

    for i, (p, g) in enumerate(pairs):
        p = p or {}
        had_error = False
        raw = g.get("raw_address") or p.get("raw_address")
        for f in SCORED_FIELDS + INFO_FIELDS:
            gv, pv = norm_value(g.get(f), strip_slash), norm_value(p.get(f), strip_slash)
            if gv is None:
                if pv is not None:
                    stats[f]["fp_unscored"] += 1
                continue
            if f in GROUPS["reference"][1] and reference_only_in_string and not in_raw(g.get(f), raw, strip_slash):
                stats[f]["skipped_not_in_string"] += 1
                continue
            stats[f]["scored"] += 1
            if pv == gv:
                stats[f]["correct"] += 1
            else:
                kind = "fn_null" if pv is None else "wrong"
                stats[f][kind] += 1
                errors[f].append({"id": g.get("id"), "raw": raw, "gold": g.get(f),
                                  "pred": p.get(f), "kind": kind})
                if f in SCORED_FIELDS:
                    had_error = True
        rec_has_error.append((p.get("confidence") or 0.0, had_error))

    acc = {f: (stats[f]["correct"] / stats[f]["scored"]) if stats[f]["scored"] else None
           for f in stats}

    # Полезен ли confidence для ручной проверки: сравниваем записи с ошибками и без
    conf_ok = [c for c, bad in rec_has_error if not bad]
    conf_bad = [c for c, bad in rec_has_error if bad]
    ranked = sorted(rec_has_error, key=lambda x: x[0])
    k = max(1, len(ranked) // 5)
    caught = sum(1 for _, bad in ranked[:k] if bad)
    k50 = min(50, len(ranked))
    conf_check = {
        "bottom50_size": k50,
        "bottom50_errors": sum(1 for _, bad in ranked[:k50] if bad),
        "error_rate_all": (len(conf_bad) / len(ranked)) if ranked else None,
        "records_with_errors": len(conf_bad),
        "mean_conf_ok": statistics.mean(conf_ok) if conf_ok else None,
        "mean_conf_err": statistics.mean(conf_bad) if conf_bad else None,
        "bottom20_size": k,
        "bottom20_errors": caught,
        "errors_caught_share": caught / len(conf_bad) if conf_bad else None,
    }

    group_scores, total, weight_sum = {}, 0.0, 0.0
    for name, (w, fields) in GROUPS.items():
        vals = [acc[f] for f in fields if acc[f] is not None]
        if vals:
            group_scores[name] = sum(vals) / len(vals)
            total += w * group_scores[name]
            weight_sum += w
    weighted = total / weight_sum if weight_sum else None
    return {"stats": stats, "accuracy": acc, "groups": group_scores,
            "weighted_estimate": weighted, "errors": errors, "n": len(gold),
            "missing": missing, "extra": len(extra),
            "confidence_check": conf_check}


def print_report(rep: dict, show_errors: int = 10) -> None:
    print(f"\nЗаписей в эталоне: {rep['n']}")
    if rep.get("missing"):
        print(f"  ВНИМАНИЕ: нет ответа для {len(rep['missing'])} записей (считаются null): {rep['missing'][:10]}")
    if rep.get("extra"):
        print(f"  ВНИМАНИЕ: {rep['extra']} лишних ответов без пары в эталоне")
    print(f"{'поле':<13}{'оцен.':>7}{'верно':>7}{'неверно':>9}{'null(FN)':>10}{'точность':>10}{'FP*':>6}")
    for f in SCORED_FIELDS + INFO_FIELDS:
        s, a = rep["stats"][f], rep["accuracy"][f]
        mark = "" if f in SCORED_FIELDS else "  (вне метрики)"
        acc_s = f"{a:.1%}" if a is not None else "—"
        print(f"{f:<13}{s['scored']:>7}{s['correct']:>7}{s['wrong']:>9}{s['fn_null']:>10}{acc_s:>10}{s['fp_unscored']:>6}{mark}")
    print("* FP — значение есть, а в эталоне null; не штрафуется по регламенту, показано справочно.")
    for name, (w, fields) in GROUPS.items():
        g = rep["groups"].get(name)
        print(f"  группа {name:<10} вес {w:.0%}: " + (f"{g:.1%}" if g is not None else "нет данных"))
    if rep["weighted_estimate"] is not None:
        print(f"  Взвешенная ОЦЕНКА: {rep['weighted_estimate']:.1%}  — НЕ официальная метрика: веса групп из "
              "регламента, агрегация внутри группы (среднее по полям) — наше допущение")
    c = rep.get("confidence_check") or {}
    if c.get("bottom50_size"):
        print(f"\nПервые {c['bottom50_size']} записей по возрастанию confidence (как в UI): ошибочных "
              f"{c['bottom50_errors']} ({c['bottom50_errors'] / c['bottom50_size']:.0%}); "
              f"в среднем по набору ошибочных {c['error_rate_all']:.0%}")
    top = Counter()
    for f in SCORED_FIELDS:
        for e in rep["errors"][f]:
            top[(f, e["kind"])] += 1
    if top:
        print("\nТоп типов ошибок (поле, вид):")
        for (f, kind), n in top.most_common(10):
            print(f"  {f:<13} {kind:<8} {n}")
    if c.get("records_with_errors"):
        print(f"\nconfidence: средний у записей без ошибок {c['mean_conf_ok']:.2f}, с ошибками {c['mean_conf_err']:.2f}; "
              f"в нижних 20% по confidence ({c['bottom20_size']} зап.) — {c['bottom20_errors']} из "
              f"{c['records_with_errors']} ошибочных ({c['errors_caught_share']:.0%})")
    elif c:
        print("\nconfidence: ошибочных записей нет — проверить полезность не на чем")
    if show_errors:
        print("\nОшибки (первые по каждому полю):")
        for f in SCORED_FIELDS:
            for e in rep["errors"][f][:show_errors]:
                print(f"  [{f}] {e['kind']:<7} gold={e['gold']!r:<28} pred={e['pred']!r:<28} | {e['raw']}")


# --------------------------------------------------------------------------
# диагностика без эталона
# --------------------------------------------------------------------------

def diagnose(parser, records: list[dict], top: int = 25) -> None:
    t0 = time.perf_counter()
    preds = parser.parse_batch(records)
    dt = time.perf_counter() - t0
    n = len(records)
    print(f"\nЗаписей: {n}, выход: {len(preds)}, время: {dt:.2f} c "
          f"({dt / max(n, 1) * 1000:.2f} мс/запись, ~{dt / max(n, 1) * 1000:.1f} c на 1000)")
    assert len(preds) == n, "КОЛИЧЕСТВО ВЫХОДНЫХ ЗАПИСЕЙ НЕ СОВПАДАЕТ"
    changed = sum(1 for r, p in zip(records, preds) if r.get("raw_address") != p.get("raw_address"))
    print(f"raw_address изменён: {changed}")

    print("\nЗаполненность полей (это НЕ точность):")
    for f in SCORED_FIELDS + INFO_FIELDS:
        filled = sum(1 for p in preds if p.get(f) not in (None, ""))
        print(f"  {f:<13} {filled:>6} ({filled / max(n, 1):.1%})")

    confs = [p.get("confidence") or 0 for p in preds]
    if confs:
        print(f"\nconfidence: медиана {statistics.median(confs):.2f}, "
              f"<0.5: {sum(c < 0.5 for c in confs)}, <0.3: {sum(c < 0.3 for c in confs)}")
        buckets = Counter(min(int(c * 10), 9) for c in confs)
        print("  " + "  ".join(f"{b / 10:.1f}+:{buckets[b]}" for b in range(10)))

    if hasattr(parser, "explain_address"):
        leftovers, flags = Counter(), Counter()
        for r in records:
            info = parser.explain_address(r.get("raw_address"))
            for w in (info.get("leftover") or "").split():
                leftovers[w.lower()] += 1
            flags.update(info.get("flags", []))
        print("\nФлаги разбора:", dict(flags.most_common()))
        print("Частые неразобранные слова (кандидаты в новые правила/справочник):")
        print("  " + ", ".join(f"{w}×{c}" for w, c in leftovers.most_common(top)))

    print(f"\n{top} записей с наименьшим confidence (на ручную проверку):")
    for p in sorted(preds, key=lambda x: x.get("confidence") or 0)[:top]:
        short = {k: p[k] for k in ("city", "street_name", "house", "apartment") if p.get(k)}
        print(f"  {p.get('confidence'):.2f} | {p.get('raw_address')!r} -> {short}")


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input")
    ap.add_argument("--gold")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--cases", help="файл кейсов в формате tests/cases.json (например tests/holdout.json)")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--parser")
    ap.add_argument("--out", help="сохранить предсказания в JSON")
    ap.add_argument("--report-json", help="сохранить отчёт (stats/accuracy/errors) в JSON")
    ap.add_argument("--show-errors", type=int, default=10)
    ap.add_argument("--keep-slash", action="store_true", help="не удалять '/' при сравнении (строже регламента)")
    ap.add_argument("--split", help="разбить размеченный файл на dev/holdout (по позициям, не по id) и выйти")
    ap.add_argument("--holdout-frac", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--split-out", default=".", help="куда писать dev.json и holdout.json")
    ap.add_argument("--baseline", help="второй парсер для таблицы «до/после» на том же эталоне")
    args = ap.parse_args()

    if args.split:
        recs = load_records(args.split)
        idx = list(range(len(recs)))
        random.Random(args.seed).shuffle(idx)
        k = round(len(recs) * args.holdout_frac)
        hold, dev = sorted(idx[:k]), sorted(idx[k:])
        out = Path(args.split_out)
        out.mkdir(parents=True, exist_ok=True)
        for name, part in (("dev.json", dev), ("holdout.json", hold)):
            (out / name).write_text(json.dumps([recs[i] for i in part], ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        print(f"{len(dev)} -> {out / 'dev.json'}, {len(hold)} -> {out / 'holdout.json'} (seed {args.seed}). "
              "holdout не открывать до финального замера.")
        return

    parser = load_parser(args.parser)

    if args.synthetic or args.cases:
        records, gold = load_synthetic(args.cases)
    elif args.input:
        records = load_records(args.input)
        gold = load_records(args.gold) if args.gold else None
    else:
        ap.error("нужно --synthetic, --cases или --input")

    if args.diagnose or gold is None:
        diagnose(parser, records)
        if gold is None:
            return

    t0 = time.perf_counter()
    preds = parser.parse_batch(records)
    dt = time.perf_counter() - t0
    print(f"Парсер: {args.parser or 'address_parser.py'}  | {len(records)} записей за {dt:.3f} c")
    if args.out:
        Path(args.out).write_text(json.dumps(preds, ensure_ascii=False, indent=2), encoding="utf-8")

    rep = evaluate(preds, gold, strip_slash=not args.keep_slash)
    print_report(rep, args.show_errors)
    if args.baseline:
        base = load_parser(args.baseline)
        brep = evaluate(base.parse_batch(records), gold, strip_slash=not args.keep_slash)
        print(f"\nДо/после ({args.baseline} -> {args.parser or 'address_parser.py'}):")
        print(f"{'поле':<13}{'до':>8}{'после':>8}")
        for f in SCORED_FIELDS:
            a, b = brep["accuracy"][f], rep["accuracy"][f]
            fmt = lambda v: f"{v:.1%}" if v is not None else "—"  # noqa: E731
            print(f"{f:<13}{fmt(a):>8}{fmt(b):>8}")
        for key, label in (("weighted_estimate", "оценка*"),):
            a, b = brep[key], rep[key]
            print(f"{label:<13}{(f'{a:.1%}' if a is not None else '—'):>8}{(f'{b:.1%}' if b is not None else '—'):>8}")
        print("* не официальная метрика (см. выше)")
    if args.report_json:
        serial = {**rep, "stats": {k: dict(v) for k, v in rep["stats"].items()}}
        Path(args.report_json).write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
